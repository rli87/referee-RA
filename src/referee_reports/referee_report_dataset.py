"""Defines the RefereeReportDataset class, which produces a report-level dataset.

    @author: Arjun Shanmugam
"""
import io
import os
import random
import warnings
from itertools import cycle
from typing import List

import matplotlib.pyplot as plt
import nltk
import numpy as np
import pandas as pd
import pkldir
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.model_selection import StratifiedKFold
from stargazer.stargazer import Stargazer

from constants import OI_constants, OutputTableConstants
from figure_utils import (plot_hist, plot_histogram, plot_pie, plot_pie_by,
                          plot_scatter)
from likelihood_ratio_model import LikelihoodRatioModel
from ols_regression import OLSRegression
from regularized_regression import RegularizedRegression


class RefereeReportDataset:
    """Builds a DataFrame at the report-level containing report,
    referee, and paper data. Allows the user to run statistical models for text analysis.
    """

    def __init__(self, path_to_pickled_reports: str, path_to_pickled_papers: str, path_to_output: str, seed: int):
        """Instantiates a RefereeReportDataset object.

        Args:
            path_to_pickled_reports (str): _description_
            path_to_pickled_papers (str): _description_
            path_to_output (str): _description_
            seed (int): _description_
        """

        self.report_df = pd.read_csv(io.StringIO(pkldir.decode(path_to_pickled_reports).decode('utf-8')))
        self.paper_df = pd.read_csv(io.StringIO(pkldir.decode(path_to_pickled_papers).decode('utf-8')))
        self.df = pd.DataFrame()
        self.path_to_output = path_to_output
        self.seed = seed
        self.report_dtm = pd.DataFrame()
        self.report_vocabulary = []
        self.tf_reports = pd.DataFrame()
        self.tf_papers = pd.DataFrame()
        self.tf_reports_adjusted = pd.DataFrame()
        self.models = {}
        self.ngrams = None

        np.random.seed(self.seed)  # Must set internal seed to ensure reproducible output from sklearn.

    def build_df(self,
                 adjust_reports_with_papers: bool,
                 normalize_documents_by_length:bool,
                 restrict_to_papers_with_mixed_gender_referees: bool,
                 balance_sample_by_categorical_column: str=None,
                 ngrams: int=1,
                 min_df:int=1,
                 max_df:float=1.0):
        self.ngrams = ngrams

        # Merge paper characteristics and referee characteristics.
        self.df = self.report_df.merge(self.paper_df,
                                                       how='inner',
                                                       left_on='paper',
                                                       right_on='paper',
                                                       suffixes=('_report', '_paper'),
                                                       validate='m:1')
        self.df = self.df.drop(columns=['Unnamed: 0_report', 'Unnamed: 0_paper'])   
        
        # Add names to columns not indicating word counts so that they can be differentiated from word count variables. 
        for column in self.df.columns:
            self.df = self.df.rename(columns={column: "_" + column + "_"})

        # Drop unneeded columnns. 
        self.df = self.df.drop(columns=['_file_type_report_', '_file_type_paper_'])


        # Save paper ID, referee ID, recommendation, and decision as categorical data.
        for column in ['_paper_', '_refnum_', '_recommendation_', '_decision_']:
            self.df[column] = self.df[column].astype('category')


        if restrict_to_papers_with_mixed_gender_referees:
            self._restrict_to_papers_with_mixed_gender_referees()

        self._build_tf_matrices(adjust_reports_with_papers=adjust_reports_with_papers,
                                ngrams=ngrams,
                                min_df=min_df,
                                max_df=max_df,
                                normalize_documents_by_length=normalize_documents_by_length) 
        
        
        if balance_sample_by_categorical_column != None:
            self._balance_sample_by_categorical_column(balance_sample_by_categorical_column)

    def _balance_sample_by_categorical_column(self, column_to_balance):
        # Get unique values and their counts for the column to balance on.
        value_counts = self.df[column_to_balance].value_counts()

        # Warn if provided column appears continuous.                                            
        if len(value_counts) > 6:
            warnings.warn("There are more than 6 unique values of the provided column. Are you sure that it is categorical?")
        
        # Check that each class appears frequently enough for us to balance observations by column.
        minimum_count = value_counts.min()
        if minimum_count < 1/(2 * len(value_counts)) * len(self.df):
            warnings.warn("The specified column contains a class which account for a small percentage of total observations. Balancing by this class would hugely reduce sample size.")
        # Balance observations.
        for index, count in value_counts.iteritems():
            # No need to drop observations which come from the lowest-frequency class. 
            if count == minimum_count:
                continue
            imbalance = count - minimum_count
            # We want to draw n random rows, where n is equal to the difference between the count of the current value
            # and the count of the least-frequent value.
            oversampled_rows = self.df.loc[self.df[column_to_balance] == index]
            num_rows_dropped = 0
            # Choose rows to drop within-paper groups at random.
            papers = self.df['_paper_'].value_counts().index.tolist()
            random.Random(self.seed).shuffle(papers)
            for paper in cycle(papers):
                current_paper_rows = oversampled_rows.loc[oversampled_rows['_paper_'] == paper]
                if len(current_paper_rows) < 2:  # Do not drop oversampled row if it is the only one within its paper group.
                    continue
                else:
                    random_row_index = current_paper_rows.sample(n=1, random_state=self.seed).index
                    self.df = self.df.drop(index=random_row_index)
                    num_rows_dropped += 1
                if num_rows_dropped == imbalance:
                    break  # Break out of this loop and consider the next oversampled category.

        self.df = self.df.reset_index(drop=True)

    def print_non_privileged_columns(self):
        """Print non-sensitive data to console. 
        """
        print(self.df[['_paper_', '_refnum_', '_recommendation_', '_decision_', '_female_']])          

    def _build_tf_matrices(self, adjust_reports_with_papers, ngrams, min_df, max_df, normalize_documents_by_length=False):
        # Fit a CountVectorizer to the reports.
        vectorizer = CountVectorizer(min_df=min_df, max_df=max_df, ngram_range=(ngrams, ngrams))
        vectorizer = vectorizer.fit(self.df['_cleaned_text_report_'])

        # Store CountVectorizer's vocabulary.
        self.report_vocabulary = list(vectorizer.get_feature_names())  # NOTE: The version of Scikit-learn installed on this machine is old. In new versions, .get_feature_names() has been deprecated.

        # Transform the reports and the papers into their T.F.V. representations.
        self.tf_reports = vectorizer.transform(self.df['_cleaned_text_report_']).toarray()
        self.tf_papers = vectorizer.transform(self.df['_cleaned_text_paper_']).toarray()

    
        # Convert the T.F.V. to DataFrames.
        self.tf_reports = pd.DataFrame(self.tf_reports, index=self.df.index, columns=self.report_vocabulary)
        self.tf_papers = pd.DataFrame(self.tf_papers, index=self.df.index, columns=self.report_vocabulary)

        # Normalize documents by length if specified.
        if normalize_documents_by_length:
            if adjust_reports_with_papers:
                self.tf_reports = self.tf_reports + 1  # Laplace smooth reports so we can calculate NR_ij/NP_j.
                self.tf_papers = self.tf_papers + 1  # Laplace smooth papers so we can calculate NR_ij/NP_j.

                report_lengths = self.tf_reports.sum(axis=1)  # Calculate NR_ij.      
                self.tf_reports = self.tf_reports.div(report_lengths, axis=0)
                paper_lengths = self.tf_papers.sum(axis=1)  # Calculate NP_j
                self.tf_papers = self.tf_papers.div(paper_lengths, axis=0)

                # Calculate NR_ij / NP_j.
                self.tf_reports_adjusted = self.tf_reports / self.tf_papers

                # Produce final DataFrame by concatenating normalized, paper-adjusted report frequencies with other data.
                self.df = pd.concat([self.df, self.tf_reports_adjusted], axis=1)
            else:
                report_lengths = self.tf_reports.sum(axis=1)  # Calculate NR_ij.      
                self.tf_reports = self.tf_reports.div(report_lengths, axis=0)

                # Produce final DataFrame by concatenating normalized report frequencies with other data.
                self.df = pd.concat([self.df, self.tf_reports], axis=1)

        else:
            if adjust_reports_with_papers:
                # Calculate the reports T.F.V., normalized by the papers T.F.V.
                self.tf_reports_adjusted = (self.tf_reports - self.tf_papers)

                # Negative values indicate that a word does not appear in the report, but does appear in the corresponding paper.
                # Replace with 0.
                self.tf_reports_adjusted[self.tf_reports_adjusted < 0] = 0
                
                # Concatenate paper-adjusted wordcounts with other data.
                self.df = pd.concat([self.df, self.tf_reports_adjusted], axis=1)
            else:
                # Concatenate report word counts with other data.
                self.df = pd.concat([self.df, self.tf_reports], axis=1)



        # Drop strings containing cleaned text.
        self.df = self.df.drop(columns=['_cleaned_text_report_', '_cleaned_text_paper_'])

    def ols_regress(self, y, X, model_name, add_constant, logistic, log_transform, standardize):
        # Check that specified independent, dependent variables are valid.
        self._validate_columns(y, X)

        # Select specified columns from DataFrame.
        dependent_variable = self.df[y]
        independent_variables = self.df[X]

        # Instantiate an OLS regression object.
        self.models[model_name] = OLSRegression(model_name=model_name,
                                                y=dependent_variable,
                                                X=independent_variables,
                                                log_transform=log_transform,
                                                standardize=standardize,
                                                add_constant=add_constant,
                                                logistic=logistic)
        self.models[model_name].fit_ols()


    def regularized_regress(self,
                            y,
                            X,
                            model_name,
                            method,
                            logistic,
                            log_transform,
                            standardize,
                            alphas,
                            cv_folds,
                            stratify,
                            add_constant,
                            sklearn=False,
                            l1_ratios=None,
                            adjust_alpha=False):
        # Check that specified independent, dependent variables are valid.
        self._validate_columns(y, X)

        # Select specified columns from DataFrame.
        dependent_variable = self.df[y]
        independent_variables = self.df[X]

        # Instantiate a RegularizedRegression object.
        self.models[model_name] = RegularizedRegression(model_name,
                                                        dependent_variable,
                                                        independent_variables,
                                                        logistic,
                                                        add_constant,
                                                        standardize,
                                                        log_transform,
                                                        alphas,
                                                        method, 
                                                        cv_folds,
                                                        stratify,
                                                        self.seed,
                                                        l1_ratios,
                                                        self.path_to_output,
                                                        adjust_alpha)
        if sklearn:
            self.models[model_name].fit_cross_validated_sklearn()
        else:                                                
            self.models[model_name].fit_cross_validated()
    
    def resample_variable_binomial(self, variable: str, p: float, ensure_balanced: bool):
        # Check that the requested column to resample is actually a column in the dataset.
        self._validate_columns(y=variable, X=[], check_length=False)

        np.random.seed(self.seed)

        female_indices = np.random.choice(np.array(np.arange(start=0, stop=len(self.df), step=1)),
                                          size=int(0.5 * len(self.df)),
                                          replace=False)
        non_female_indices = self.df.index.difference(female_indices)
        self.df.loc[female_indices, variable] = 1
        self.df.loc[non_female_indices, variable] = 0



    def _produce_summary_statistics(self,
                                    adjust_reports_with_papers: bool,
                                    normalize_documents_by_length: bool
                                    ):
        # Pie chart: number of papers with mixed-gender refereeship vs. all female refereeship vs. all male refereeship
        mean_female_by_paper = ( self.df.groupby(by='_paper_', observed=True)
                                 .mean()['_female_']  # Get mean of female within each group.
                               )
        mean_female_by_paper[(mean_female_by_paper != 0) & (mean_female_by_paper != 1)] = "Refereed by Females and Non-Females"        
        mean_female_by_paper = mean_female_by_paper.replace({0: "Refereed by Non-Females Only", 1: "Refereed by Females Only"})    
  
        mean_female_by_paper_grouped = mean_female_by_paper.groupby(mean_female_by_paper).count()  # Get counts of each unique value        
        plot_pie(x=mean_female_by_paper_grouped,
                 filepath=os.path.join(self.path_to_output, 'pie_referee_genders_by_paper.png'),
                 title="Gender Breakdowns of Paper Referees")

        # Pie chart: number of referees on each paper.
        referees_per_paper = ( self.df.groupby(by='_paper_', observed=True)
                                .count()
                                .mean(axis=1)  # Every column in this DataFrame is identical, so mean across axes to reduce.
                                .astype(int)  
                                .replace({1: "One Referee",
                                                         2: "Two Referees",
                                                         3: "Three Referees",
                                                         4: "Four Referees"})
                             )  
        referees_per_paper_grouped = referees_per_paper.groupby(referees_per_paper).count()  # Get counts of each unique value.
        plot_pie(x=referees_per_paper_grouped,
                 filepath=os.path.join(self.path_to_output, 'pie_num_referees_per_paper.png'),
                 title="Number of Referees Per Paper")  
        
        # Histogram: Skewness of the word count variables.
        if adjust_reports_with_papers:
            title = "Skewness of Paper-Adjusted Wordcount Variables (" + str(self.ngrams) + "-grams)"
            filepath = os.path.join(self.path_to_output, "hist_paper_adjusted_" + str(self.ngrams)+ "_grams_counts_skewness.png")
        else:
            title = "Skewness of Raw Wordcount Variables (" + str(self.ngrams) + "-grams)"
            filepath = os.path.join(self.path_to_output, "hist_raw_" + str(self.ngrams)+ "_grams_counts_skewness.png")
        skewness = self.df[self.report_vocabulary].skew(axis=0)
        plot_histogram(x=skewness,
                       filepath=filepath,
                       title=title,
                       xlabel="""Skewness of Variable
                    
                    This figure calculates 
                    
                    """)

        # Histogram: Skewness of log(x+1)-transformed word count variables.
        if adjust_reports_with_papers:
            title = "Skewness of ln(x+1)-Transformed Paper-Adjusted Wordcount Variables (" + str(self.ngrams) + "-grams)"
            filepath = os.path.join(self.path_to_output, "hist_paper_adjusted_" + str(self.ngrams)+ "_grams_counts_skewness_log_transformed.png")
        else:
            title = "Skewness of ln(x+1)-Transformed Raw Wordcount Variables (" + str(self.ngrams) + "-grams)"
            filepath = os.path.join(self.path_to_output, "hist_raw_" + str(self.ngrams)+ "_grams_counts_skewness_log_transformed.png")
        skewness = np.log(self.df[self.report_vocabulary] + 1).skew(axis=0)
        plot_histogram(x=skewness,
                       filepath=filepath,
                       title=title,
                       xlabel="Skewness of Variable")

        # Pie chart: Breakdown of decision and recommendation
        decision_breakdown = self.df['_decision_'].value_counts()
        plot_pie(x=decision_breakdown,
                 filepath=os.path.join(self.path_to_output, 'pie_decision.png'),
                 title="Breakdown of Referee Decisions")

        recommendation_breakdown = self.df['_recommendation_'].value_counts()
        plot_pie(x=recommendation_breakdown,
                 filepath=os.path.join(self.path_to_output, 'pie_recomendation.png'),
                 title="Breakdown of Referee Recommendation")
        
        # Pie chart: Breakdown of referee decision and recommendation by gender.
        plot_pie_by(x=self.df['_decision_'],
                    by=self.df['_female_'],
                    filepath=os.path.join(self.path_to_output, "pie_decision_by_female.png"),
                    title="Breakdown of Referee Decision by Referee Gender",
                    by_value_labels={0: "Non-Female", 1: "Female"})

        plot_pie_by(x=self.df['_recommendation_'],
                    by=self.df['_female_'],
                    filepath=os.path.join(self.path_to_output, "pie_recommendation_by_female.png"),
                    title="Breakdown of Referee Recommendation by Referee Gender",
                    by_value_labels={0: "Non-Female", 1: "Female"})

        # Pie chart: Breakdown of the number and gender of referees for each paper.
        referee_counts_each_gender = (self.df.groupby(by=['_paper_', '_female_'], observed=True)
                                        .count()  # Get number of referees of each gender for each paper.
                                        .mean(axis=1)  # Counts are identical across columns, so take mean across columns to reduce.
                                        .unstack()  # Reshape from long to wide.
                                        .value_counts()  # Get counts of each permutation.
        )
        # Assign new index so that helper function can generate a labeled pie chart.
        referee_counts_each_gender = pd.Series(referee_counts_each_gender.values,
                                               index=['One Non-Female, One Female', 'Two Non-Female, One Female', 'One Non-Female, Two Female'])
        plot_pie(x=referee_counts_each_gender,
                 filepath=os.path.join(self.path_to_output, "pie_referee_gender_number_permutations.png"),
                 title="Breakdown of Paper Referees and their Genders")

        if not normalize_documents_by_length:
            # Histogram: Number of tokens in reports.
            tokens_per_report = self.tf_reports[self.report_vocabulary].sum(axis=1)
            xlabel = '''Length (''' + str(self.ngrams) +'''-Gram Tokens)
        
            Note: This figure is a histogram of the number of tokens in the sample reports after all cleaning, 
            sample restriction, vectorization, and removal of low-frequency tokens. Equivalently, this figure
            gives the distribution of report lengths in tokens immediately before analysis.
                    '''
            plot_histogram(x=tokens_per_report,
                           filepath=os.path.join(self.path_to_output,
                                              "hist_tokens_per_report_immediately_before_analysis.png"),
                           title="Number of Tokens in Each Report (" + str(self.ngrams) + "-Grams)",
                           xlabel=xlabel)

            # Histogram: Number of tokens in papers.
            tokens_per_paper = self.tf_papers[self.report_vocabulary].sum(axis=1)
            xlabel = '''Length (''' + str(self.ngrams) +'''-Gram Tokens)

            Note: This figure is a histogram of the number of tokens in the sample papers after cleaning, 
            sample restriction, restriction to introductions, removal of thank yous, vectorization, and
            removal of low-frequency tokens. Equivalently, this figure gives the distribution of paper
            lengths in tokens immediately before analysis. Note that this figure only counts tokens 
            which also appear somewhere in the corpus of reports.
            '''
            plot_histogram(x=tokens_per_paper,
                           filepath=os.path.join(self.path_to_output,
                                                 "hist_tokens_per_paper_immediately_before_analysis.png"),
                           title="Number of Tokens in Each Paper (" + str(self.ngrams) + "-Grams)",
                           xlabel=xlabel)

        # """

        # # Histogram: Counts and log counts of token in the paper-adjusted D.T.M. for the reports.
        # if adjust_reports_with_papers:
        #     if normalize_documents_by_length:
        #         # Histogram: Frequencies in the paper-adjusted D.T.M. for the reports.
        #         token_counts = self.tf_reports_adjusted[self.report_vocabulary].to_numpy().flatten()
        #         xlabel = """Values in Paper-Adjusted Matrix
                            
        #             Note: This figure is a histogram of the values in the normalized
        #             paper-adjusted reports matrix. To calculate this figure, I first
        #             add one to every cell of the reports D.T.M. and the papers D.T.M.
        #             Then, I normalize each cell of the reports D.T.M. by the sum of
        #             the row containing that cell. I normalize each cell of the papers
        #             D.T.M. in the same way. Then, each row (i, j) of the normalized
        #             reports D.T.M. is divided element-wise by row j of the
        #             normalized papers D.T.M.
        #             """
        #         plot_histogram(x=pd.Series(token_counts),
        #                     filepath=os.path.join(self.path_to_output, "hist_paper_adjusted_matrix_immediately_before_analysis.png"),
        #                     title="Distribution of Paper-Adjusted Token Frequencies in Reports (" + str(self.ngrams) + "-Grams)",
        #                     xlabel=xlabel)
        #         # Histogram: Log frequencies in the paper-adjusted D.T.M. for the reports.
        #         token_counts = np.log(self.tf_reports_adjusted[self.report_vocabulary] + 1).to_numpy().flatten()
        #         xlabel = """Values in Paper-Adjusted Matrix
                            
        #                     Note: This figure is a histogram of the values in the normalized
        #                     paper-adjusted reports matrix. To calculate this figure, I first
        #                     add one to every cell of the reports D.T.M. and the papers D.T.M.
        #                     Then, I normalize each cell of the reports D.T.M. by the sum of
        #                     the row containing that cell. I normalize each cell of the papers
        #                     D.T.M. in the same way. Then, each row (i, j) of the normalized
        #                     reports D.T.M. is divided element-wise by row j of the normalized
        #                     papers D.T.M. Lastly, I add 1 to each cell of this matrix and then
        #                     take the base ten log of each cell.
        #                     """
        #         plot_histogram(x=pd.Series(token_counts),
        #                     filepath=os.path.join(self.path_to_output, "hist_log_paper_adjusted_matrix_immediately_before_analysis.png"),
        #                     title="Distribution of Log-Transformed Paper-Adjusted Token Frequencies in Reports (" + str(self.ngrams) + "-Grams)",
        #                     xlabel=xlabel)
        #     else:
        #         # Histogram: Counts in the paper-adjusted D.T.M. for the reports.
        #         token_counts = self.tf_reports_adjusted[self.report_vocabulary].to_numpy().flatten()
        #         xlabel = """Values in Paper-Adjusted Matrix
                                                        
        #                     Note: This figure is a histogram of the values in the paper-adjusted
        #                     reports D.T.M. To produce the paper-adjusted reports D.T.M., I
        #                     subtract from each row (i, j) of the reports D.T.M. row j of the
        #                     papers D.T.M.
        #                     """
        #         plot_histogram(x=pd.Series(token_counts),
        #                     filepath=os.path.join(self.path_to_output, "hist_paper_adjusted_matrix_immediately_before_analysis.png"),
        #                     title="Distribution of Paper-Adjusted Token Counts in Reports (" + str(self.ngrams) + "-Grams)",
        #                     xlabel=xlabel)
        #         # Histogram: Log counts in the paper-adjusted D.T.M. for the reports.
        #         token_counts = np.log(self.tf_reports_adjusted[self.report_vocabulary] + 1).to_numpy().flatten()
        #         xlabel = """Values in Paper-Adjusted Matrix
                            
        #                     Note: This figure is a histogram of the values in the paper-adjusted
        #                     reports D.T.M. To produce the paper-adjusted reports D.T.M., I
        #                     subtract from each row (i, j) of the reports D.T.M. row j of the
        #                     papers D.T.M. Then, I add 1 to each cell of this D.T.M. and then
        #                     take the base ten log of each cell.
        #                     """
        #         plot_histogram(x=pd.Series(token_counts),
        #                     filepath=os.path.join(self.path_to_output, "hist_log_paper_adjusted_matrix_immediately_before_analysis.png"),
        #                     title="Distribution of Log Paper-Adjusted Token Counts in Reports (" + str(self.ngrams) + "-Grams)",
        #                     xlabel=xlabel)

        
        # # Histogram: Counts of each token in the reports.
        # token_counts = self.tf_reports[self.report_vocabulary].sum(axis=0)
        # plot_histogram(x=token_counts,
        #                filepath=os.path.join(self.path_to_output,
        #                                      "hist_token_counts_immediately_before_analysis_reports.png"),
        #                title="Distribution of Total Appearances Across Tokens in Reports (" + str(self.ngrams) + "-Grams)",
        #                xlabel='''Number of Times Token Appears
                       
        #                         This figure plots the distribution of tokens' total apperances across reports.
        #                         To produce this figure, I first calculate the total number of times each token appears
        #                         across all reports. I then bin tokens according to those sums and display the results
        #                         above.
        #                ''')
        
        # # Histogram: Counts of each token in the papers.
        # token_counts = self.tf_papers[self.report_vocabulary].sum(axis=0)
        # plot_histogram(x=token_counts,
        #                filepath=os.path.join(self.path_to_output,
        #                                      "hist_token_counts_immediately_before_analysis_papers.png"),
        #                title="Distribution of Total Apperances Across Tokens in Papers (" + str(self.ngrams) + "-Grams)",
        #                xlabel='''Number of Times Token Appears
                       
        #                This figure plots the distribution of tokens' total appearances across papers.
        #                To produce this figure, I first calculate the total number of times each token appears
        #                across all papers. I then bin tokens according to those sums and display the results above.
        #                ''')

        # # Histogram: Counts of each token in the reports, log(x+1) transformed.
        # token_counts = np.log(self.tf_reports[self.report_vocabulary] + 1).sum(axis=0) 
        # plot_histogram(x=token_counts,
        #                filepath=os.path.join(self.path_to_output,
        #                                      "hist_token_log_counts_immediately_before_analysis_reports.png"),
        #                title="Distribution of log(x+1)-Transformed Counts Across Reports (" + str(self.ngrams) + "-Grams)",
        #                xlabel='''Sum of log(x+1)-Transformed Counts
                       
        #                This figure plots the distribution of tokens' log(x+1)-transformed counts,
        #                summed across reports. To produce this figure, I add 1 to each cell of the 
        #                D.T.M. for the reports. Then, I take the natural log of each cell. Lastly,
        #                I sum the D.T.M. across reports (rows) and bin the tokens according to the
        #                resulting sums.
        #                ''')
        
        # # Histogram: Counts of each token in the papers, log(x+1) transformed.
        # token_counts = np.log(self.tf_papers[self.report_vocabulary] + 1).sum(axis=0)
        # plot_histogram(x=token_counts,
        #                filepath=os.path.join(self.path_to_output,
        #                                      "hist_token_log_counts_immediately_before_analysis_papers.png"),
        #                title="Distribution of log(x+1)-Transformed Counts Across Papers (" + str(self.ngrams) + "-Grams)",
        #                xlabel='''Sum of log(x+1)-Transformed Counts
                       
        #                This figure plots the distribution of tokens' log(x+1)-transformed counts, 
        #                summed across papers. To produce this figure, I add 1 to each cell of the 
        #                D.T.M. for the reports. Then, I take the natural log of each cell. Lastly,
        #                I sum the D.T.M. across papers (rows) and bin the tokens according to the
        #                resulting sums.
        #                ''')
                       
        # """

        # Histogram: Number of reports in which tokens appear.
        num_reports_where_word_appears = (self.tf_reports
                                              .mask(self.tf_reports > 0, 1)
                                              .sum(axis=0)
                                              .transpose()
            )

        xlabel = """Number of Reports

        This figure plots the distribution over the number of reports in which each token appears.
        It is produced after all cleaning but before the removal of low-frequency tokens. To produce this figure,
        I calculate the number of reports in which each token appears at least once. I sort tokens into bins
        based on those values to produce this figure. """
        plot_histogram(x=num_reports_where_word_appears,
                           title="Number of Reports in Which Tokens Appear (" + str(self.ngrams) + "-grams)",
                           xlabel=xlabel,
                           filepath=os.path.join(self.path_to_output, 'hist_num_reports_where_tokens_appear_' + str(self.ngrams) + '_grams.png'))

        # Hist: Average length of male vs. female reports immediately before analysis.
        report_lengths = (
            self.tf_reports
            .sum(axis=1)
        )
        male_report_lengths = report_lengths.loc[self.df['_female_'] == 0]
        male_report_lengths_mean = male_report_lengths.mean().round(3)
        male_report_lengths_se = male_report_lengths.sem().round(3)
        female_report_lengths = report_lengths.loc[self.df['_female_'] == 1]
        female_report_lengths_mean = female_report_lengths.mean().round(3)
        female_report_lengths_se = female_report_lengths.sem().round(3)

        fig, ax = plt.subplots(1, 1)
        plot_hist(ax=ax,
                  x=male_report_lengths,
                  title="Distribution of Male-Written vs. Female-Written Report Lengths",
                  xlabel='Report Length',
                  ylabel='Normalized Frequency',
                  alpha=0.5,
                  color=OI_constants.male_color.value,
                  summary_statistics_linecolors=OI_constants.male_color.value,
                  label="Male Reports \n Mean length: " + str(male_report_lengths_mean) + " (SE: " + str(male_report_lengths_se) + ")",
                  summary_statistics=['median'])
        plot_hist(ax=ax,
                  x=female_report_lengths,
                  title="Distribution of Male-Written vs. Female-Written Report Lengths",
                  xlabel='Report Length',
                  ylabel='Normalized Frequency',
                  alpha=0.5,
                  color=OI_constants.female_color.value,
                  summary_statistics_linecolors=OI_constants.female_color.value,
                  label="Female Reports \n Mean length: " + str(female_report_lengths_mean) + " (SE: " + str(female_report_lengths_se) + ")",
                  summary_statistics=['median'])
        ax.legend(loc='best', fontsize='x-small')
        plt.savefig(os.path.join(self.path_to_output, "hist_male_vs_female_report_lengths_" + str(self.ngrams) + "_grams.png"), bbox_inches='tight')
        plt.close(fig)

        # Table: Most common words for men and women (R).
        occurrences_by_gender = (
            pd.concat([self.tf_reports, self.df['_female_']], axis=1)
            .groupby(by='_female_')
            .sum()
            .transpose()
        )
        
        male_occurrences = pd.Series(occurrences_by_gender[0].sort_values(ascending=False), name="Most Common Words in Male-Written Reports").reset_index().iloc[:50]
        male_occurrences = (male_occurrences['index'] + ": " + male_occurrences["Most Common Words in Male-Written Reports"].astype(str)).rename("\textbf{Most Common Words in Male-Written Reports}")
        female_occurrences = pd.Series(occurrences_by_gender[1].sort_values(ascending=False), name="Most Common Words in Female-Written Reports").reset_index().iloc[:50]
        female_occurrences = (female_occurrences['index'] + ": " + female_occurrences["Most Common Words in Female-Written Reports"].astype(str)).rename("\textbf{Most Common Words in Female-Written Reports}")
        pd.concat([male_occurrences, female_occurrences], axis=1).to_latex(os.path.join(self.path_to_output, "table_most_common_words_by_gender_R_" + str(self.ngrams) + "_grams.tex"),
                                                                           index=False,
                                                                           escape=False,
                                                                           float_format="%.3f")

        # Table: Most common words for men and women (NR).
        report_lengths = self.tf_reports.sum(axis=1)  # Calculate NR_ij.      
        occurrences_by_gender = (
            pd.concat([self.tf_reports.div(report_lengths, axis=0), self.df['_female_']], axis=1)
            .groupby(by='_female_')
            .sum()
            .transpose()
        )
        male_occurrences = pd.Series(occurrences_by_gender[0].sort_values(ascending=False), name="Most Common Words in Male-Written Reports").reset_index().iloc[:50].round(3)
        male_occurrences = (male_occurrences['index'] + ": " + male_occurrences["Most Common Words in Male-Written Reports"].astype(str)).rename("\textbf{Most Common Words in Male-Written Reports}")
        female_occurrences = pd.Series(occurrences_by_gender[1].sort_values(ascending=False), name="Most Common Words in Female-Written Reports").reset_index().iloc[:50].round(3)
        female_occurrences = (female_occurrences['index'] + ": " + female_occurrences["Most Common Words in Female-Written Reports"].astype(str)).rename("\textbf{Most Common Words in Female-Written Reports}")
        pd.concat([male_occurrences, female_occurrences], axis=1).to_latex(os.path.join(self.path_to_output, "table_most_common_words_by_gender_NR_" + str(self.ngrams) + "_grams.tex"),
                                                                           index=False,
                                                                           escape=False,
                                                                           float_format="%.3f")


        
        # Hist: Cosine similarity between NR vectors and NP vectors, separately for males and females.
        report_lengths = self.tf_reports.sum(axis=1)  # Calculate NR_ij.      
        NR = self.tf_reports.div(report_lengths, axis=0)
        paper_lengths = self.tf_papers.sum(axis=1)  # Calculate NP_j
        NP = self.tf_papers.div(paper_lengths, axis=0)
        index = pd.MultiIndex.from_frame(self.df[['_paper_', '_refnum_']], names=['_paper_', '_refnum_'])
        columns = self.df['_paper_']
        cosine_similarities = pd.DataFrame(cosine_similarity(NR, NP), index=index, columns=columns)
        cosine_similarities = pd.Series(np.diag(cosine_similarities), index=cosine_similarities.index, name='similarity')
        genders = self.df[['_female_', '_paper_', '_refnum_']].set_index(['_paper_', '_refnum_'])
        cosine_similarities_and_genders = pd.concat([cosine_similarities, genders], axis=1)
        cosine_similarities_males = cosine_similarities_and_genders.loc[cosine_similarities_and_genders['_female_'] == 0, 'similarity']
        cosine_similarities_females = cosine_similarities_and_genders.loc[cosine_similarities_and_genders['_female_'] == 1, 'similarity']
        fig, ax = plt.subplots(1, 1)

        male_mean = cosine_similarities_males.mean().round(3)
        male_se = cosine_similarities_males.sem().round(3)
        plot_hist(ax=ax,
                  x=cosine_similarities_males,
                  title="Distribution of Cosine Similarity Between Reports and Their Associated Papers",
                  xlabel='Cosine Similarity',
                  ylabel='Normalized Frequency',
                  alpha=0.5,
                  color=OI_constants.male_color.value,
                  summary_statistics_linecolors=OI_constants.male_color.value,
                  label="Male Reports \n Mean Cosine Similarity: " + str(male_mean) + " (SE: " + str(male_se) + ")",
                  summary_statistics=['median'])

        female_mean = cosine_similarities_females.mean().round(3)
        female_se = cosine_similarities_females.sem().round(3)  
        plot_hist(ax=ax,
                  x=cosine_similarities_females,
                  title="Distribution of Cosine Similarity Between Reports and Their Associated Papers",
                  xlabel='Cosine Similarity',
                  ylabel='Normalized Frequency',
                  alpha=0.5,
                  color=OI_constants.female_color.value,
                  summary_statistics_linecolors=OI_constants.female_color.value,
                  label="Female Reports \n Mean Cosine Similarity: " + str(female_mean) + " (SE: " + str(female_se) + ")",
                  summary_statistics=['median'])
        ax.legend(loc='best', fontsize='x-small')
        plt.savefig(os.path.join(self.path_to_output, "hist_male_vs_female_cosine_similarity_NR_NP_" + str(self.ngrams) + "_grams.png"), bbox_inches='tight')
        plt.close(fig)


    def get_vocabulary(self):
        return self.report_vocabulary

    def build_regularized_results_table(self, model_name, num_coefs_to_report=30):
        self._validate_model_request(model_name, expected_model_type="Regularized")
        
        # Store results table.
        results_table = self.models[model_name].get_results_table()
        
        # Validate requested number of top/bottom coefficients.
        if len(results_table) < num_coefs_to_report * 2:
            raise ValueError("There are fewer than " + str(num_coefs_to_report * 2) + " coefficients in the results table.")

        # Store metrics in a named column.
        metrics = pd.Series(results_table.loc[OutputTableConstants.REGULARIZED_REGRESSION_METRICS.value], name="Metrics")
        if not self.models[model_name].add_constant:
            metrics = metrics.drop(labels="Constant")
        if not self.models[model_name].adjust_alpha:
            metrics = metrics.drop(labels="$\\alpha^{*}_{adjusted}$")
            metrics = metrics.drop("$\\bar{p}_{\\alpha^{*}_{adjusted}}$")
        if self.models[model_name].method != 'elasticnet':
            metrics = metrics.drop(labels="Optimal L1 Penalty Weight")
        metrics = metrics.reset_index()
        metrics = metrics['index'] + ": " + metrics['Metrics'].round(3).astype(str)
        



        # Store specified dummy variables in a named column.
        dummy_coefficients = pd.Series(results_table.loc[self.models[model_name].dummy_variables], name="Dummy Variables").reset_index()
        dummy_coefficients = dummy_coefficients['index'] + ": " + dummy_coefficients['Dummy Variables'].round(3).astype(str)                    
        
        # Drop non-coefficients from the Series of coefficients and sort coefficients by value.
        non_text_coefs = self.models[model_name].dummy_variables + OutputTableConstants.REGULARIZED_REGRESSION_METRICS.value
        coefficients_sorted = results_table.drop(labels=non_text_coefs).sort_values(ascending=False)

        # Get largest nonzero coefficients; concatenate them with the associated token.
        top_coefficients = pd.Series(coefficients_sorted.iloc[:num_coefs_to_report], name="Largest " + str(num_coefs_to_report) + " Coefficients").reset_index()
        top_coefficients = top_coefficients[top_coefficients["Largest " + str(num_coefs_to_report) + " Coefficients"] != 0]
        top_coefficients = top_coefficients['index'] + ": " + top_coefficients["Largest " + str(num_coefs_to_report) + " Coefficients"].round(3).astype(str)
        
        # Get smallest nonzero coefficients; concatenate them with the associated token.
        bottom_coefficients = pd.Series(coefficients_sorted.iloc[-num_coefs_to_report:], name="Smallest " + str(num_coefs_to_report) + " Coefficients").reset_index()
        bottom_coefficients = bottom_coefficients[bottom_coefficients["Smallest " + str(num_coefs_to_report) + " Coefficients"] != 0]
        bottom_coefficients = bottom_coefficients['index'] + ": " + bottom_coefficients["Smallest " + str(num_coefs_to_report) + " Coefficients"].round(3).astype(str)
        bottom_coefficients = bottom_coefficients.iloc[::-1].reset_index(drop=True)  # Flip order.


        results_table = pd.concat([top_coefficients, bottom_coefficients, dummy_coefficients, metrics], axis=1)
        results_table = results_table.fillna(' ')
        results_table.columns = ['Words Most Predictive of Female Referee',
                                 'Words Most Predictive of Non-Female Referee',
                                 'Dummy Variables',
                                 'Other Metrics']
        if len(dummy_coefficients) == 0:
            results_table = results_table.drop(columns='Dummy Variables')
        results_table.columns = ['\textbf{' + column + '}' for column in results_table.columns]
        results_table.to_latex(os.path.join(self.path_to_output, model_name + "_results.tex"),
                               index=False,
                               escape=False,
                               float_format="%.3f")
    
    def build_ols_results_table(self,
                                filename: str,
                                requested_models: List[str],
                                title: str=None,
                                show_confidence_intervals=False,
                                dependent_variable_name=None,
                                column_titles=None,
                                show_degrees_of_freedom=False,
                                rename_covariates=None,
                                covariate_order=None,
                                lines_to_add=None,
                                custom_notes=None):
        # Validate model names.
        for model_name in requested_models:
            if not model_name in self.models:
                raise ValueError("A model by that name has not been estimated.")

        # Validate model types.
        for model_name in requested_models:
            if self.models[model_name].get_model_type() != "OLS":
                raise TypeError("This function may only be used to produce output for regularized models.")

        # Grab results tables.
        results = []
        for model_name in requested_models:
            self._validate_model_request(model_name, expected_model_type="OLS")
            current_result = self.models[model_name].get_results_table()
            results.append(current_result)
        
        # Build table with Stargazer.
        stargazer = Stargazer(results)
        

        # Edit table.
        if title is not None:
            stargazer.title(title)
        stargazer.show_confidence_intervals(show_confidence_intervals)
        if dependent_variable_name is not None:
            stargazer.dependent_variable_name(dependent_variable_name)
        stargazer.show_degrees_of_freedom(show_degrees_of_freedom)
        if rename_covariates is not None:
            stargazer.rename_covariates(rename_covariates)
        if covariate_order is not None:
            stargazer.covariate_order(covariate_order)
        if lines_to_add is not None:
            for line in lines_to_add:
                stargazer.add_line(line[0], line[1], line[2])
        if custom_notes is not None:
            stargazer.add_custom_notes(custom_notes)
        if column_titles is not None:
            stargazer.custom_columns(column_titles[0], column_titles[1])

        # Write LaTeX.
        latex = stargazer.render_latex()

        # Write to file.
        with open(os.path.join(self.path_to_output, filename + ".tex"), "w") as output_file:
            output_file.write(latex)


        html = stargazer.render_html()
        # TODO: Delete this! Just for viewing purposes as I debug.
        with open(os.path.join(self.path_to_output, filename + ".html"), "w") as html_output_file:
            html_output_file.write(html)



    def _validate_columns(self, y, X, check_length=True):
        columns = self.df.columns.tolist()
        
        if (y not in columns):
            error_msg = "The specified dependent variable is not a variable in the dataset."
            raise ValueError(error_msg)
        elif (not set(X).issubset(set(columns))):
            error_msg = "One or more of the specified independent variables is not a variable in the dataset."
            raise ValueError(error_msg)
        elif len(X) == 0 and check_length == True:
            error_msg = "You must specify at least one independent variable."
            raise ValueError(error_msg)

    def add_column(self, column):
        if not isinstance(column, pd.Series):
            error_msg = "The passed column is not a pandas Series."
            raise ValueError(error_msg)
        elif len(column) != len(self.df):
            error_msg = "The specified column must contain the same number of rows as the existing dataset."
            raise ValueError(error_msg)
        elif column.index.tolist() != self.df.index.tolist():
            error_msg = "The specified column must have an idential pandas Index compared to the existing dataset."
            raise ValueError(error_msg)
        elif column.name in set(self.df.columns.tolist()):
            error_msg = "The specified column's name must not be identical to an existing column in the dataset."
            raise ValueError(error_msg)
        else:
            self.df = pd.concat([self.df, column], axis=1)

    def _restrict_to_papers_with_mixed_gender_referees(self):
        # Get the portion of each paper's referees who are female.
        refereeship_gender_breakdown = pd.Series(self.df.groupby('_paper_', observed=True)['_female_'].transform('mean'),
                                                 name='gender_breakdown')

        # Drop rows where 0% or 100% of referees are female.
        all_female_or_all_male = refereeship_gender_breakdown.loc[(refereeship_gender_breakdown == 0) | (refereeship_gender_breakdown == 1)]

        self.df = self.df.drop(index=all_female_or_all_male.index).reset_index(drop=True)

    def calculate_likelihood_ratios(self, model_name: str, model_type: str):
        self.models[model_name] = LikelihoodRatioModel(dtm=self.df[self.report_vocabulary],
                                                       document_classification_variable=self.df['_female_'],
                                                       fe_variable=self.df['_paper_'],
                                                       model_type=model_type)                                                                                                                                                                                            
        self.models[model_name].fit()

    def _validate_model_request(self, model_name, expected_model_type):
        # Validate model name.
        if not model_name in self.models:
            raise ValueError("A model by that name has not been estimated.")

        # Validate model type.
        if expected_model_type not in self.models[model_name].get_model_type():
            raise TypeError("This function may only be used to produce output for " + expected_model_type + " models.")

    def build_likelihood_results_table(self, model_name, num_ratios_to_report=40, decimal_places=3):
        self._validate_model_request(model_name, "Likelihood Ratio")
        
        # Store results table.
        results_table = self.models[model_name].get_results_table()

        # Build results table for pooled estimates.=================================================
        pooled_columns = ["pooled_ratios",
                          "frequency_over_documents",
                          "frequency_over_fe_groups"]
        # Round likelihood ratios and word frequencies over reports and papers.
        results_table.loc[:, "pooled_ratios"] = results_table["pooled_ratios"].round(decimal_places)
        sorted_pooled_ratios = (results_table[pooled_columns]
                                .sort_values(by='pooled_ratios')  # Sort likelihood ratios. 
                                .reset_index()  # Reset index, containing the words corresponding to each ratio, and add it as a column.
                                )                                    
        # Get highest pooled likelihood ratios.
        highest_pooled_ratios = sorted_pooled_ratios.iloc[-num_ratios_to_report:]
        # Concatenate words with their corresponding likelihood ratios.
        highest_pooled_ratios.loc[:, 'Highest Pooled Ratios'] = (highest_pooled_ratios['index'] +
                                                                                               ": " +
                                                                                               highest_pooled_ratios['pooled_ratios'].astype(str)
                                                                                               )
        # Get lowest likelihood ratios.
        lowest_pooled_ratios = sorted_pooled_ratios.iloc[:num_ratios_to_report]
        # Concatenate words with their corresponding likelihood ratios.
        lowest_pooled_ratios.loc[:, 'Lowest Pooled Ratios'] = (lowest_pooled_ratios['index'] +
                                                                                             ": " +
                                                                                             lowest_pooled_ratios['pooled_ratios'].astype(str)
                                                                                             )


        # Build results table for within-paper estimates.===========================================
        fe_columns = ["fe_ratios",
                      "frequency_over_documents",
                      "frequency_over_fe_groups"]
        # Round likelihood ratios.
        results_table.loc[:, 'fe_ratios'] = results_table['fe_ratios'].round(decimal_places)
        sorted_fe_ratios = (results_table[fe_columns]
                            .sort_values(by='fe_ratios')  # Sort likelihood ratios.
                            .reset_index()  # Reset index, containing the words corresponding to each ratio, and add it as a column.
        )
        # Get highest likelihood ratios.
        highest_fe_ratios = sorted_fe_ratios.iloc[-num_ratios_to_report:]
        # Concatenate words with their corresponding likelihood ratios.
        highest_fe_ratios.loc[:, "Highest Within-Paper Ratios"] = (highest_fe_ratios['index'] +
                                                                                                   ": " +
                                                                                                   highest_fe_ratios['fe_ratios'].astype(str)
                                                                                                   )        
        # Get lowest likelihood ratios.
        lowest_fe_ratios = sorted_fe_ratios.iloc[:num_ratios_to_report]
        # Concatenate words with their corresponding likelihood ratios.
        lowest_fe_ratios.loc[:, "Lowest Within-Paper Ratios"] = (lowest_fe_ratios['index'] +
                                                                                                 ": " +
                                                                                                 lowest_fe_ratios["fe_ratios"].astype(str)
                                                                                                 )  

        # Plot likelihood ratios by the frequency of the associated tokens.
        extreme_pooled_ratios = pd.concat([highest_pooled_ratios['pooled_ratios'],
                                           lowest_pooled_ratios['pooled_ratios']],
                                          axis=0)
        extreme_fe_ratios = pd.concat([highest_fe_ratios['fe_ratios'],
                                       lowest_fe_ratios['fe_ratios']],
                                      axis=0)
        pooled_ratios_frequencies = pd.concat([highest_pooled_ratios['frequency_over_documents'],
                                               lowest_pooled_ratios['frequency_over_documents']],
                                              axis=0)
        fe_ratios_frequencies = pd.concat([highest_fe_ratios['frequency_over_documents'],
                                           lowest_fe_ratios['frequency_over_documents']],
                                          axis=0)    
        fig, axs = plt.subplots(2, 1, sharex='col')            
        y_list = [extreme_pooled_ratios,
                  extreme_fe_ratios]
        x_list = [pooled_ratios_frequencies,
                 fe_ratios_frequencies]
        xlabels = ["",
                   """Portion of Documents in Which Token Appears

                   This figure plots likelihood ratios by the frequency of the associated tokens across paper groups, separately for
                   the most extreme pooled sample and within-paper likelihood ratios. A \'paper group\' is defined as a group of
                   reports associated with a single paper. To produce this figure, pooled sample and within-paper likelihood ratios 
                   are first calculated for every token. These likelihood ratios become the y-values of the points displayed in this graph.
                   Then, for each of the most extreme likelihood ratios, I calculate the portion of paper groups in which the associated
                   token appears at least once. Those values form the x-values of the points displayed in this graph. Note that both
                   plots share the same x-axis. 
                   """]
        ylabels = ["Likelihood Ratio", "Likelihood Ratio"]
        titles = ["Most Extreme Pooled Sample Likelihood Ratios by Frequency of Associated Tokens",
                  "Most Extreme Within-Paper Likelihood Ratios by Frequency of Associated Tokens"]
        colors = [OI_constants.p1.value, OI_constants.p3.value]
        for ax, x, y, xlabel, ylabel, title, color in zip(axs, x_list, y_list, xlabels, ylabels, titles, colors):
            ax.scatter(x, y, c=color, s=2)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.set_title(title)
        plt.savefig(os.path.join(self.path_to_output, 'scatter_ratios_by_frequency_' + str(self.ngrams) + '_grams.png'),
                    bbox_inches='tight')

        # Produce final results table.
        highest_df = (pd.concat([highest_pooled_ratios['Highest Pooled Ratios'],
                                 highest_fe_ratios["Highest Within-Paper Ratios"]],
                               axis=1)
                               .iloc[::-1]
                               .reset_index(drop=True)
                      )
        lowest_df = (pd.concat([lowest_pooled_ratios['Lowest Pooled Ratios'],
                                lowest_fe_ratios["Lowest Within-Paper Ratios"]],
                               axis=1).reset_index(drop=True)
                     )

        pd.set_option('display.max_colwidth', -1)
        metrics = results_table['Metrics'].reset_index(drop=True).loc[:3]
        results  = pd.concat([lowest_df, highest_df, metrics], axis=1)
        results.columns = ["\textbf{" + column + "}" for column in results.columns]
        results.to_latex(os.path.join(self.path_to_output, model_name + "_ratio_results.tex"), index=False, escape=False)

        