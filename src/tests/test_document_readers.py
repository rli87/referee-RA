"""Tests PaperReader functionality.
"""
from referee_reports.document_readers import PaperReader
import os
from io import StringIO
import pandas as pd
import pkldir
import pytest


@pytest.fixture
def paper_reader():
    return PaperReader("../../data/raw/papers-pkl", "../../data/intermediate")

def test__choose_optimal_format(paper_reader):
    # Test on simple DataFrame containing four identical fake files saved in different formats.
    df = pd.DataFrame.from_dict({'filename_without_extension': ['doc_1', 'doc_1', 'doc_1', 'doc_1',
                                                                'doc_2', 'doc_2', 'doc_2', 'doc_2'],
                                 'full_filename': ['doc_1.docx.pkl', 'doc_1.pdf.pkl', 'doc_1.txt.pkl', 'doc_1.md.pkl',
                                                   'doc_2.docx.pkl', 'doc_2.pdf.pkl', 'doc_2.txt.pkl', 'doc_2.md.pkl'],
                                 'file_type': ['docx', 'pdf', 'txt', 'md',
                                               'docx', 'pdf', 'txt', 'md']})
    actual = df.groupby('filename_without_extension')[['full_filename', 'file_type']].apply(paper_reader._choose_optimal_format)
    expected = pd.DataFrame.from_dict({'filename_without_extension': ['doc_1', 'doc_2'],
                                       'full_filename': ['doc_1.pdf.pkl', 'doc_2.pdf.pkl'],
                                       'file_type': ['pdf', 'pdf']}).set_index('filename_without_extension')
    pd.testing.assert_frame_equal(actual, expected)

    # Test on a DataFrame containing three identical fake files saved in different formats, none of which are given as arguments in the method call.
    df = pd.DataFrame.from_dict({'filename_without_extension': ['doc_1', 'doc_1', 'doc_1'],
                                 'full_filename': ['doc_1.csv.pkl', 'doc_1.py.pkl', 'doc_1.pages.pkl'],
                                 'file_type': ['csv', 'py', 'pages']})
    with pytest.raises(FileNotFoundError):
        df.groupby('filename_without_extension')[['full_filename', 'file_type']].apply(paper_reader._choose_optimal_format)


def test__get_string_with_most_occurrences(paper_reader_with_nathans_paper_only, paper_reader_with_test_papers_only):
    paper_reader_with_nathans_paper_only._decode_text()
    paper_reader_with_nathans_paper_only._restrict_to_intro()

    with open('/data/home/ashanmu1/Desktop/refereebias/code/test_assets/JPUBE-D-99-99999_thank_yous.txt', 'r') as f:
        expected = f.read()
        keywords = ['thanks',
                    'thank',
                    'manuscript',
                    'indebted',
                    'comments',
                    'discussion',
                    'NBER',
                    'excellent',
                    'research assistance',
                    'helpful',
                    'expressed in this paper',
                    'errors',
                    'disclaimer',
                    'grant',
                    '@']
    actual = (paper_reader_with_nathans_paper_only.df['text']
    .str.split(pat="\n\n")
    .apply(lambda strings: paper_reader_with_nathans_paper_only._get_string_with_most_occurrences(strings=strings,
                                                                                                  keywords=keywords))
    .iloc[0]
    )

    assert actual == expected, "The strings do not match!"

    # Print thank you sections from each test paper to make sure that the algorithm is working as intended.
    paper_reader_with_test_papers_only.build_df()

    with open("/data/home/ashanmu1/Desktop/refereebias/code/cleaned_introductions.txt", 'w') as f:
        for text, filename in zip(paper_reader_with_test_papers_only.df['cleaned_text'], paper_reader_with_test_papers_only.df['filename']):
            f.write(filename + "\n" + text + "\n\n\n\n")

    paper_reader_with_test_papers_only._decode_text()
    with open("/data/home/ashanmu1/Desktop/refereebias/code/firm_choices.txt", 'w') as f:
        f.write(paper_reader_with_test_papers_only.df.loc[paper_reader_with_test_papers_only.df['filename'] == 'FirmChoices-RES.pdf.pkl']['text'].iloc[0])


def test__remove_thank_yous(paper_reader_with_nathans_paper_only):
    # Define dummy keywords.
    keywords = ['keyword 1', 'keyword 2']

    # Define dummy data.
    paper_reader_with_nathans_paper_only.df['text'] = pd.Series(["Section 1 of Text\n\nSection 2 of Text keyword 1 keyword 2\n\nSection 3 of Text",
                                                                 "Section 1 of Text\n\nSection 2 of Text keyword 1\n\nSection 3 of Text\n\nSection 4 of Text keyword 1 keyword 1\n\nSection 5 of Text"],
                                                                index=range(2))

    paper_reader_with_nathans_paper_only._remove_thank_yous(keywords=keywords)
    expected = pd.Series(["Section 1 of Text\n\n\n\nSection 3 of Text",
                          "Section 1 of Text\n\nSection 2 of Text keyword 1\n\nSection 3 of Text\n\n\n\nSection 5 of Text"])
    actual = paper_reader_with_nathans_paper_only.df['text']
    pd.testing.assert_series_equal(actual, expected, check_names=False)


def test__decode_text(paper_reader):
    # Check that every row of the 'text' column contains a string.
    paper_reader._decode_text()
    expected_output = pd.Series(str, index=paper_reader.df.index)
    pd.testing.assert_series_equal(paper_reader.df['text'].apply(lambda row: type(row)), expected_output, check_names=False)


def test__tokenize_text(paper_reader):
    paper_reader._decode_text()
    paper_reader._tokenize_text(report=False)

    # Check that 'cleaned_text' is entirely alphanumeric besides spaces.
    true_series = pd.Series(True, index=paper_reader.df['cleaned_text'].index, name="Series of Only Trues")
    contains_non_alphanumeric = pd.Series(paper_reader.df['cleaned_text'].str.replace(" ", "", regex=False).str.isalnum(), name="Contains Only-Alphanumerics")
    pd.testing.assert_series_equal(contains_non_alphanumeric, true_series, check_names=False)


def test__mwe_retokenize(paper_reader):
    tokens = ["The", "coefficient", "on", "\\", "beta", "is", "3", ".", "4", "and", "\\", "gamma", "equals", "2", "."]
    retokenized_list = paper_reader._mwe_retokenize(tokens, "\\")
    assert retokenized_list == ["The", "coefficient", "on", "\\beta", "is", "3", ".", "4", "and", "\\gamma", "equals", "2", "."]

    tokens = ["The", "coefficient", "on", "\\", "\\", "beta", "is", "3", ".", "4", "and", "\\", "gamma", "equals", "2", "."]
    retokenized_list = paper_reader._mwe_retokenize(tokens, "\\")
    assert paper_reader._mwe_retokenize(tokens, "\\") == ["The", "coefficient", "on", "\\\\beta", "is", "3", ".", "4", "and", "\\gamma", "equals", "2", "."]

    tokens = ["The", "coefficient", "on", "\\", "\\", "\\", "beta", "is", "3", ".", "4", "and", "\\", "\\", "gamma", "\\", "equals", "2", ".", "\\"]
    retokenized_list = paper_reader._mwe_retokenize(tokens, "\\")
    assert paper_reader._mwe_retokenize(tokens, "\\") == ["The", "coefficient", "on", "\\\\\\beta", "is", "3", ".", "4", "and", "\\\\gamma", "\\equals", "2",
                                                          ".", "\\"]

    tokens = ["The", "coefficient", "on", "\\", "\\", "\\", "beta", "is", "3", ".", "4", "and", "\\", "\\", "gamma", "\\", "equals", "2", ".", "\\", "\\"]
    retokenized_list = paper_reader._mwe_retokenize(tokens, "\\")
    assert paper_reader._mwe_retokenize(tokens, "\\") == ["The", "coefficient", "on", "\\\\\\beta", "is", "3", ".", "4", "and", "\\\\gamma", "\\equals", "2",
                                                          ".", "\\\\"]


def test__get_count_per_group_of_sentences(paper_reader):
    # Define list to test on.
    ungrouped_sentences = ['Sentence 1', 'Sentence 2 WORD', 'Sentence 3 woRd',
                           'Sentence 4', 'Sentence 5 word', 'Sentence 6',
                           'sentence 7', 'Sentence 8', 'Sentence 9 word',
                           'Sentence 10']
    expected_output = pd.Series([2, 2, 2, 1, 1, 0, 1, 1])
    pd.testing.assert_series_equal(paper_reader._get_count_per_group_of_sentences(ungrouped_sentences, 3, minimum_count=2, word="word")[0], expected_output,
                                   check_names=False)
    assert paper_reader._get_count_per_group_of_sentences(ungrouped_sentences, 3, minimum_count=2, word="word")[1] == 3


def test__restrict_to_intro(paper_reader):
    paper_reader._decode_text()
    paper_reader._restrict_to_intro()

    # Check that all introductions are of a reasonable length.
    for i in range(len(paper_reader.df['introduction_length'])):
        if not (35 < paper_reader.df['introduction_length'].iloc[i] < 200):
            print("Paper number " + str(i) + "'s introduction is " + str(
                paper_reader.df['introduction_length'].iloc[i]) + " sentences long. Note that this may not be an error.")




def test__pickle_df(paper_reader):
    # Create test data, assign it to the PaperReader's DataFrame, and pickle it.
    paper_reader.build_df()

    # Decode the pickled data and store it in a DataFrame.
    decoded_bytes = pkldir.decode(os.path.join(paper_reader.path_to_intermediate_data, "papers.txt.pkl"))
    decoded_string = decoded_bytes.decode('utf-8')
    decoded_df = pd.read_csv(StringIO(decoded_string)).drop(columns=['Unnamed: 0'])

    # Check that the original DataFrame and the DataFrame we generated from the pickle are identical.
    pd.testing.assert_frame_equal(paper_reader.df, decoded_df)
