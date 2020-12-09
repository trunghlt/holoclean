from enum import Enum
from holoclean.utils import NULL_REPR
import logging

from tqdm import tqdm
import pandas as pd
import numpy as np


class Source(Enum):
    FILE = 1
    DF = 2
    DB = 3
    SQL = 4


class Table:
    """
    A wrapper class for Dataset Tables.
    """

    def __init__(
        self,
        name,
        src,
        na_values=None,
        exclude_attr_cols=["_tid_"],
        fpath=None,
        df=None,
        table_query=None,
        db_engine=None,
    ):
        """
        :param name: (str) name to assign to dataset.
        :param na_values: (str or list[str]) values to interpret as NULL.
        :param exclude_attr_cols: (list[str]) list of columns to NOT treat as
            attributes during training/learning.
        :param src: (Source) type of source to load from. Note additional
            parameters MUST be provided for each specific source:
                Source.FILE: :param fpath:, read from CSV file
                Source.DF: :param df:, read from pandas DataFrame
                Source.DB: :param db_engine:, read from database table with :param name:
                Source.SQL: :param table_query: and :param db_engine:, use result
                    from :param table_query:

        :param fpath: (str) File path to CSV file containing raw data
        :param df: (pandas.DataFrame) DataFrame contain the raw ingested data
        :param schema_name: (str) Schema used while loading Source.DB
        :param table_query: (str) sql query to construct table from
        :param db_engine: (DBEngine) database engine object
        """
        self.name = name
        self.index_count = 0
        # Copy the list to memoize
        self.exclude_attr_cols = list(exclude_attr_cols)
        self.df = pd.DataFrame()
        self.df_raw = pd.DataFrame()  # data before normalized - not lower all string

        if src == Source.FILE:
            if fpath is None:
                raise Exception(
                    "ERROR while loading table. File path for CSV file name expected. Please provide <fpath> param."
                )
            # TODO(richardwu): use COPY FROM instead of loading this into memory
            self.df = pd.read_csv(
                fpath, dtype=str, na_values=na_values, encoding="utf-8"
            )
            self.df_raw = pd.read_csv(
                fpath, na_values=na_values, encoding="utf-8"
            )

            # Normalize the dataframe: drop null columns, convert to lowercase strings, and strip whitespaces.
            for attr in self.df.columns.values:
                if self.df[attr].isnull().all():
                    logging.warning(
                        "Dropping the following null column from the dataset: '%s'",
                        attr,
                    )
                    self.df.drop(labels=[attr], axis=1, inplace=True)
                    continue
                if attr in exclude_attr_cols:
                    continue

                self.df[attr] = self.df[attr].str.strip().str.lower()
        elif src == Source.DF:
            if df is None:
                raise Exception(
                    "ERROR while loading table. Dataframe expected. Please provide <df> param."
                )
            self.df = df
        elif src == Source.DB:
            if db_engine is None:
                raise Exception(
                    "ERROR while loading table. DB connection expected. Please provide <db_engine>."
                )
            with db_engine.engine.connect() as conn:
                self.df = pd.read_sql_table(name, con=conn, schema=db_engine.dbschema)
        elif src == Source.SQL:
            if table_query is None or db_engine is None:
                raise Exception(
                    "ERROR while loading table. SQL Query and DB connection expected. Please provide <table_query> and <db_engine>."
                )
            db_engine.create_db_table_from_query(self.name, table_query)
            with db_engine.engine.connect() as conn:
                self.df = pd.read_sql_table(name, con=conn, schema=db_engine.dbschema)

    def _revert_normalized_value(self, df_raw: pd.DataFrame) -> pd.DataFrame:
        # indicate it's not the data table
        if df_raw.empty:
            return self.df

        df_reverted = pd.DataFrame()

        logging.info("Reverting normalized values")
        r_size, _ = self.df.shape

        for attr in tqdm(self.df.columns.values):
            if attr in self.exclude_attr_cols:
                df_reverted[attr] = self.df[attr]
            else:
                for indx in range(r_size):
                    raw_value = df_raw[attr].iloc[indx]
                    repair_value = self.df[attr].iloc[indx]
                    if (
                        str(raw_value).strip().lower()
                        == str(repair_value).strip().lower()
                    ):
                        df_reverted.at[indx, attr] = raw_value
                    else:
                        df_reverted.at[indx, attr] = np.nan if repair_value == NULL_REPR else repair_value

        return df_reverted

    def store_to_db(
        self,
        db_conn,
        df_raw: pd.DataFrame = pd.DataFrame(),
        if_exists="replace",
        index=False,
        index_label=None,
        schema=None,
    ):
        df = self._revert_normalized_value(df_raw)
        
        df.to_sql(
            self.name,
            db_conn,
            if_exists=if_exists,
            index=index,
            index_label=index_label,
            schema=schema,
        )

    def get_attributes(self):
        """
        get_attributes returns the columns that are trainable/learnable attributes
        (i.e. exclude meta-columns like _tid_).
        """
        if self.df.empty:
            raise Exception(
                "Empty Dataframe associated with table {name}. Cannot return attributes.".format(
                    name=self.name
                )
            )
        return list(col for col in self.df.columns if col not in self.exclude_attr_cols)

    def create_df_index(self, attr_list):
        self.df.set_index(attr_list, inplace=True)

    def create_db_index(self, db_engine, attr_list):
        index_name = "{name}_{idx}".format(name=self.name, idx=self.index_count)
        db_engine.create_db_index(index_name, self.name, attr_list)
        self.index_count += 1
