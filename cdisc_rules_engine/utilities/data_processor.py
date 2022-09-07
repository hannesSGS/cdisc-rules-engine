import asyncio
from collections import Counter
from datetime import datetime
from typing import Generator, List, Optional, Set, Union, Tuple
from uuid import uuid4

import pandas as pd

from cdisc_rules_engine.config import config
from cdisc_rules_engine.exceptions.custom_exceptions import InvalidMatchKeyError
from cdisc_rules_engine.models.dictionaries.meddra.meddra_variables import (
    MedDRAVariables,
)
from cdisc_rules_engine.models.dictionaries.meddra.terms.meddra_term import MedDRATerm
from cdisc_rules_engine.models.dictionaries.meddra.terms.term_types import TermTypes
from cdisc_rules_engine.models.dictionaries.whodrug import WhodrugRecordTypes
from cdisc_rules_engine.models.operation_params import OperationParams
from cdisc_rules_engine.services.cache.cache_service_factory import CacheServiceFactory
from cdisc_rules_engine.interfaces import (
    CacheServiceInterface,
    DataServiceInterface,
)
from cdisc_rules_engine.services.cdisc_library_service import CDISCLibraryService
from cdisc_rules_engine.services.data_services import (
    DataServiceFactory,
    DummyDataService,
)
from cdisc_rules_engine.utilities.utils import (
    get_corresponding_datasets,
    get_operations_cache_key,
    get_rules_cache_key,
    is_split_dataset,
    search_in_list_of_dicts,
    get_meddra_code_term_pairs_cache_key,
)


class DataProcessor:
    def __init__(self, data_service=None, cache: CacheServiceInterface = None):
        self.cache = cache or CacheServiceFactory(config).get_cache_service()
        self.data_service = (
            data_service or DataServiceFactory(config, self.cache).get_data_service()
        )

    def min(self, params: OperationParams) -> Union[int, float, pd.DataFrame]:
        if not params.grouping:
            return params.dataframe[params.target].min()
        else:
            return params.dataframe.groupby(params.grouping, as_index=False).min()

    def max(self, params: OperationParams) -> Union[int, float, pd.DataFrame]:
        if not params.grouping:
            return params.dataframe[params.target].max()
        else:
            return params.dataframe.groupby(params.grouping, as_index=False).max()

    def mean(self, params: OperationParams) -> Union[int, float, pd.DataFrame]:
        if not params.grouping:
            return params.dataframe[params.target].mean()
        else:
            return params.dataframe.groupby(params.grouping, as_index=False).mean()

    def distinct(
        self,
        params: OperationParams,
    ) -> Union[pd.Series, pd.DataFrame]:
        if not params.grouping:
            data = params.dataframe[params.target].unique()
            if isinstance(data[0], bytes):
                data = data.astype(str)
            data_converted_to_set = set(data)
            return pd.Series([data_converted_to_set] * len(params.dataframe))
        else:
            grouped = params.dataframe.groupby(params.grouping, as_index=False)
            return grouped[params.target].agg(lambda x: pd.Series([set(x.unique())]))

    def min_date(self, params: OperationParams) -> Union[str, pd.DataFrame]:
        if not params.grouping:
            data = pd.to_datetime(params.dataframe[params.target])
            min_date = data.min()
            if isinstance(min_date, pd._libs.tslibs.nattype.NaTType):
                return ""
            else:
                return min_date.isoformat()
        else:
            return params.dataframe.groupby(params.grouping, as_index=False).min()

    def max_date(self, params: OperationParams) -> Union[str, pd.DataFrame]:
        if not params.grouping:
            data = pd.to_datetime(params.dataframe[params.target])
            max_date = data.max()
            if isinstance(max_date, pd._libs.tslibs.nattype.NaTType):
                return ""
            else:
                return max_date.isoformat()
        else:
            return params.dataframe.groupby(params.grouping, as_index=False).max()

    def dy(self, params: OperationParams) -> pd.Series:
        dtc_value = params.dataframe[params.target].map(datetime.fromisoformat)
        rfstdtc_value = params.dataframe["RFSTDTC"].map(datetime.fromisoformat)

        # + 1 if --DTC is on or after RFSTDTC
        delta = (dtc_value - rfstdtc_value).map(
            lambda x: x.days if x.days < 0 else x.days + 1
        )
        return delta

    def extract_metadata(self, params: OperationParams) -> pd.Series:
        """
        Extracts domain metadata for the given target and returns it as a Series.
        """
        # get metadata
        metadata: pd.DataFrame = self.data_service.get_dataset_metadata(
            dataset_name=params.dataset_path
        )

        # extract target value. Metadata df always has one row
        target_value = metadata.get(params.target, pd.Series())[0]
        return pd.Series([target_value] * len(params.dataframe))

    def variable_exists(self, params: OperationParams) -> bool:
        """
        Returns a boolean if the target, is a variable in the dataset.
        """
        return params.target in params.dataframe

    def variable_value_count(self, params: OperationParams) -> int:
        """
        Returns a boolean if the target, is a variable in any dataset in the study.
        """
        cache_key = get_operations_cache_key(
            params.directory_path,
            operation_name=f"study_value_count_{params.target}",
        )
        variable_value_count = (
            self.cache.get(cache_key)
            if not DataProcessor.is_dummy_data(self.data_service)
            else None
        )
        if not variable_value_count:
            variable_value_count = asyncio.run(
                DataProcessor.get_all_study_variable_value_counts(
                    params.target,
                    params.directory_path,
                    self.data_service,
                    params.datasets,
                )
            )
            if not DataProcessor.is_dummy_data(self.data_service):
                # Only cache when not using dummy dataservice,
                # so that subsequent calls with different data are not cached.
                self.cache.add(cache_key, variable_value_count)
        return variable_value_count

    def variable_names(self, params: OperationParams) -> set:
        """
        Return the set of variable names for the given standard
        """
        cache_service_obj = CacheServiceFactory(config).get_cache_service()
        cache_key = get_rules_cache_key(params.standard, params.standard_version)
        variable_details: dict = cache_service_obj.get(cache_key)
        if variable_details is not None:
            return set(variable_details.keys())
        else:
            cdisc_library_service = CDISCLibraryService(config, cache_service_obj)
            return set(
                cdisc_library_service.get_variables_details(
                    params.standard, params.standard_version
                ).keys()
            )

    def valid_meddra_code_references(self, params: OperationParams) -> pd.Series:
        if not params.meddra_path:
            raise ValueError("Can't execute the operation, no meddra path provided")
        code_variables = [
            MedDRAVariables.SOCCD.value,
            MedDRAVariables.HLGTCD.value,
            MedDRAVariables.HLTCD.value,
            MedDRAVariables.PTCD.value,
            MedDRAVariables.LLTCD.value,
        ]
        code_strings = [f"{params.domain}{variable}" for variable in code_variables]
        cache_key = f"meddra_valid_code_hierarchies_{params.meddra_path}"
        valid_code_hierarchies = self.cache.get(cache_key)
        if not valid_code_hierarchies:
            terms: dict = self.cache.get(params.meddra_path)
            valid_code_hierarchies = MedDRATerm.get_code_hierarchies(terms)
            self.cache.add(cache_key, valid_code_hierarchies)
        column = str(uuid4()) + "_codes"
        params.dataframe[column] = params.dataframe[code_strings].agg("/".join, axis=1)
        result = params.dataframe[column].isin(valid_code_hierarchies)
        return result

    def valid_meddra_term_references(self, params: OperationParams) -> pd.Series:
        if not params.meddra_path:
            raise ValueError("Can't execute the operation, no meddra path provided")
        code_variables = [
            MedDRAVariables.SOC.value,
            MedDRAVariables.HLGT.value,
            MedDRAVariables.HLT.value,
            MedDRAVariables.DECOD.value,
            MedDRAVariables.LLT.value,
        ]
        code_strings = [f"{params.domain}{variable}" for variable in code_variables]
        cache_key = f"meddra_valid_term_hierarchies_{params.meddra_path}"
        valid_term_hierarchies = self.cache.get(cache_key)
        if not valid_term_hierarchies:
            terms: dict = self.cache.get(params.meddra_path)
            valid_term_hierarchies = MedDRATerm.get_term_hierarchies(terms)
            self.cache.add(cache_key, valid_term_hierarchies)
        column = str(uuid4()) + "_terms"
        params.dataframe[column] = params.dataframe[code_strings].agg("/".join, axis=1)
        result = params.dataframe[column].isin(valid_term_hierarchies)
        return result

    def valid_meddra_code_term_pairs(self, params: OperationParams) -> pd.Series:
        if not params.meddra_path:
            raise ValueError("Can't execute the operation, no meddra path provided")
        term_type, columns = self._get_columns_by_meddra_variable_name(params)
        cache_key: str = get_meddra_code_term_pairs_cache_key(params.meddra_path)
        valid_code_term_pairs = self.cache.get(cache_key)
        if not valid_code_term_pairs:
            terms: dict = self.cache.get(params.meddra_path)
            valid_code_term_pairs = MedDRATerm.get_code_term_pairs(terms)
            self.cache.add(cache_key, valid_code_term_pairs)
        column = str(uuid4()) + "_pairs"
        params.dataframe[column] = list(
            zip(
                params.dataframe[columns[0]],
                params.dataframe[columns[1]],
            )
        )
        result = params.dataframe[column].isin(valid_code_term_pairs[term_type])
        return result

    def valid_whodrug_references(self, params: OperationParams) -> pd.Series:
        """
        Checks if a reference to whodrug term points
        to the existing code in Atc Text (INA) file.
        """
        if not params.whodrug_path:
            raise ValueError("Can't execute the operation, no whodrug path provided")

        terms: dict = self.cache.get(params.whodrug_path)
        valid_codes: Generator = (
            term.code for term in terms[WhodrugRecordTypes.ATC_TEXT.value]
        )
        return params.dataframe[params.target].isin(valid_codes)

    def _get_columns_by_meddra_variable_name(
        self, params: OperationParams
    ) -> Optional[Tuple[str, Tuple[str, str]]]:
        """
        Extracts target name from params and
        returns associated term type and dataset columns.
        """
        variable_pair_map = {
            f"{params.domain}{MedDRAVariables.SOC.value}": (
                TermTypes.SOC.value,
                (
                    f"{params.domain}{MedDRAVariables.SOCCD.value}",
                    f"{params.domain}{MedDRAVariables.SOC.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.SOCCD.value}": (
                TermTypes.SOC.value,
                (
                    f"{params.domain}{MedDRAVariables.SOCCD.value}",
                    f"{params.domain}{MedDRAVariables.SOC.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.HLGT.value}": (
                TermTypes.HLGT.value,
                (
                    f"{params.domain}{MedDRAVariables.HLGTCD.value}",
                    f"{params.domain}{MedDRAVariables.HLGT.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.HLGTCD.value}": (
                TermTypes.HLGT.value,
                (
                    f"{params.domain}{MedDRAVariables.HLGTCD.value}",
                    f"{params.domain}{MedDRAVariables.HLGT.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.HLT.value}": (
                TermTypes.HLT.value,
                (
                    f"{params.domain}{MedDRAVariables.HLTCD.value}",
                    f"{params.domain}{MedDRAVariables.HLT.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.HLTCD.value}": (
                TermTypes.HLT.value,
                (
                    f"{params.domain}{MedDRAVariables.HLTCD.value}",
                    f"{params.domain}{MedDRAVariables.HLT.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.DECOD.value}": (
                TermTypes.PT.value,
                (
                    f"{params.domain}{MedDRAVariables.PTCD.value}",
                    f"{params.domain}{MedDRAVariables.DECOD.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.PTCD.value}": (
                TermTypes.PT.value,
                (
                    f"{params.domain}{MedDRAVariables.PTCD.value}",
                    f"{params.domain}{MedDRAVariables.DECOD.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.LLT.value}": (
                TermTypes.LLT.value,
                (
                    f"{params.domain}{MedDRAVariables.LLTCD.value}",
                    f"{params.domain}{MedDRAVariables.LLT.value}",
                ),
            ),
            f"{params.domain}{MedDRAVariables.LLTCD.value}": (
                TermTypes.LLT.value,
                (
                    f"{params.domain}{MedDRAVariables.LLTCD.value}",
                    f"{params.domain}{MedDRAVariables.LLT.value}",
                ),
            ),
        }
        return variable_pair_map.get(params.target)

    @staticmethod
    async def get_dataset_variables(study_path, dataset, data_service) -> Set:
        data = data_service.get_dataset(f"{study_path}/{dataset.get('filename')}")
        return set(data.columns)

    @staticmethod
    async def get_all_study_variables(study_path, data_service, datasets) -> Set:
        coroutines = [
            DataProcessor.get_dataset_variables(study_path, dataset, data_service)
            for dataset in datasets
        ]
        dataset_variables: List[Set] = await asyncio.gather(*coroutines)
        return set().union(*dataset_variables)

    @staticmethod
    async def get_dataset_variable_value_count(
        target: str, study_path: str, datasets: List[dict], dataset: dict, data_service
    ) -> Counter:
        domain = dataset.get("domain")
        if is_split_dataset(datasets, domain):
            files = [
                f"{study_path}/{dataset.get('filename')}"
                for dataset in get_corresponding_datasets(datasets, domain)
            ]
            data: pd.DataFrame = data_service.join_split_datasets(
                data_service.get_dataset, files
            )
        else:
            data: pd.DataFrame = data_service.get_dataset(
                f"{study_path}/{dataset.get('filename')}"
            )
        target_variable = target.replace("--", domain, 1)
        if target_variable in data:
            return Counter(data[target_variable].unique())
        else:
            return Counter()

    @staticmethod
    async def get_all_study_variable_value_counts(
        target, study_path, data_service, datasets
    ) -> dict:
        """
        Returns a mapping of variable values to the number
        of times that value appears in the study.
        """
        datasets_with_unique_domains = list(
            {dataset["domain"]: dataset for dataset in datasets}.values()
        )
        coroutines = [
            DataProcessor.get_dataset_variable_value_count(
                target, study_path, datasets, dataset, data_service
            )
            for dataset in datasets_with_unique_domains
        ]
        dataset_variable_value_counts: List[Counter] = await asyncio.gather(*coroutines)
        return dict(sum(dataset_variable_value_counts, Counter()))

    @staticmethod
    def get_unique_record(dataframe):
        if len(dataframe.index) > 1:
            raise InvalidMatchKeyError("Match key did not return a unique record")
        return dataframe.iloc[0]

    def preprocess_relationship_dataset(
        self, dataset_path: str, dataset: pd.DataFrame, datasets: List[dict]
    ) -> dict:
        # Get unique RDOMAINS and corresponding ID Var
        reference_data = {}
        if "RDOMAIN" in dataset:
            rdomains = dataset["RDOMAIN"].unique()
            idvar_column_values = self.get_column_values(dataset, "IDVAR").unique()
            reference_data = self.async_get_reference_data(
                dataset_path, datasets, idvar_column_values, rdomains
            )
        elif "RSUBJID" in dataset:
            # get USUBJID from column in DM dataset
            reference_data = self.get_column_data(
                dataset_path, datasets, ["USUBJID"], "DM"
            )
            if "USUBJID" in reference_data.get("DM", {}):
                reference_data["DM"]["RSUBJID"] = reference_data["DM"]["USUBJID"]
                del reference_data["DM"]["USUBJID"]
        return reference_data

    def get_column_values(self, dataset, column):
        if column in dataset:
            return dataset[column]
        return []

    def get_columns(self, dataset, columns):
        column_data = {}
        for column in columns:
            if column in dataset:
                column_data[column] = dataset[column].values
        return column_data

    def get_column_data(
        self, dataset_path: str, datasets: List[dict], columns: list, domain: str
    ):
        reference_data = {}
        domain_details: dict = search_in_list_of_dicts(
            datasets, lambda item: item.get("domain") == domain
        )
        if domain_details:
            data_filename = f"{dataset_path}/{domain_details['filename']}"
            new_data = self.data_service.get_dataset(dataset_name=data_filename)
            reference_data[domain] = self.get_columns(new_data, columns)
        return reference_data

    async def async_get_column_data(self, dataset_path, datasets, columns, domain):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.get_column_data, dataset_path, datasets, columns, domain
        )

    def async_get_reference_data(
        self, dataset_path, datasets: List[dict], columns, domains
    ):
        coroutines = [
            self.async_get_column_data(dataset_path, datasets, columns, domain)
            for domain in domains
        ]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reference_data = {}
        data = loop.run_until_complete(asyncio.gather(*coroutines))
        for column in data:
            reference_data = {**reference_data, **column}
        return reference_data

    @staticmethod
    def filter_dataset_by_match_keys_of_other_dataset(
        dataset: pd.DataFrame,
        dataset_match_keys: List[str],
        other_dataset: pd.DataFrame,
        other_dataset_match_keys: List[str],
    ) -> pd.DataFrame:
        """
        Returns a DataFrame where values of match keys of
        dataset are equal to values of match keys of other dataset.
        Example:
            dataset = USUBJID  DOMAIN
                      CDISC001 AE
                      CDISC001 AE
                      CDISC009 AE
            dataset_match_keys = ["USUBJID"]
            other_dataset = USUBJID  DOMAIN
                            CDISC009 AE
            other_dataset_match_keys = ["USUBJID"]

            The result will be: USUBJID  DOMAIN
                                CDISC009 AE
        """
        dataset_w_ind = dataset.set_index(dataset_match_keys)
        other_dataset_w_ind = other_dataset.set_index(other_dataset_match_keys)
        condition = dataset_w_ind.index.isin(other_dataset_w_ind.index)
        return dataset_w_ind[condition].reset_index()

    @staticmethod
    def filter_parent_dataset_by_supp_dataset(
        parent_dataset: pd.DataFrame,
        supp_dataset: pd.DataFrame,
        column_with_names: str,
        column_with_values: str,
    ) -> pd.DataFrame:
        """
        A wrapper function for convenient filtering of parent dataset by supp dataset.
        Does two things:
        1. Filters parent dataset by RDOMAIN column of supp dataset.
        2. Filters parent dataset by columns of supp dataset
           that describe their relation.
        """
        parent_dataset = DataProcessor.filter_parent_dataset_by_supp_dataset_rdomain(
            parent_dataset, supp_dataset
        )
        return DataProcessor.filter_dataset_by_nested_columns_of_other_dataset(
            parent_dataset, supp_dataset, column_with_names, column_with_values
        )

    @staticmethod
    def filter_parent_dataset_by_supp_dataset_rdomain(
        parent_dataset: pd.DataFrame, supp_dataset: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Leaves only those rows in parent dataset
        where DOMAIN is the same as RDOMAIN of supp dataset.
        """
        parent_domain_values: pd.Series = parent_dataset.get("DOMAIN", pd.Series())
        supp_domain_values: pd.Series = supp_dataset.get("RDOMAIN", pd.Series())
        if parent_domain_values.empty or supp_domain_values.empty:
            return parent_dataset

        return parent_dataset[parent_domain_values.isin(supp_domain_values)]

    @staticmethod
    def filter_dataset_by_nested_columns_of_other_dataset(
        dataset: pd.DataFrame,
        other_dataset: pd.DataFrame,
        column_with_names: str,
        column_with_values: str,
    ) -> pd.DataFrame:
        """
        other_dataset has two columns:
            1. list of column names which exist in dataset - column_with_names.
            2. list of values of the aforementioned columns - column_with_values.

        The function filters columns name of dataset by their respective column values.

        Example:
            other_dataset = IDVAR IDVARVAL
                            ECSEQ 100
                            ECSEQ 101
                            ECNUM 105
            column_with_names = "IDVAR"
            column_with_values = "IDVARVAL"

            We need to leave only those rows in dataset
            where dataset["ECSEQ"] is equal to 100 or 101
            AND dataset["ECNUM"] is equal to 105.
        """
        grouped = other_dataset.groupby(column_with_names, group_keys=False)

        def filter_dataset_by_group_values(group) -> pd.DataFrame:
            decimal_group_values: pd.Series = (
                group[column_with_values].astype(float).astype(str)
            )
            decimal_dataset_values: pd.Series = (
                dataset[group.name].astype(float).astype(str)
            )
            condition: pd.Series = decimal_dataset_values.isin(decimal_group_values)
            return dataset[condition]

        result = grouped.apply(lambda group: filter_dataset_by_group_values(group))
        # grouping breaks sorting, need to sort once again
        return result.sort_values(list(grouped.groups.keys()))

    @staticmethod
    def merge_datasets_on_relationship_columns(
        left_dataset: pd.DataFrame,
        right_dataset: pd.DataFrame,
        right_dataset_domain_name: str,
        column_with_names: str,
        column_with_values: str,
    ) -> pd.DataFrame:
        """
        Uses full join to merge given datasets on the
        columns that describe their relation.
        """
        # right dataset holds column names of left dataset.
        # all values in the column are the same
        left_ds_col_name: str = right_dataset[column_with_names][0]

        # convert numeric columns to one data type to avoid merging errors
        # there is no point in converting string cols since their data type is the same
        DataProcessor.cast_numeric_cols_to_same_data_type(
            right_dataset, column_with_values, left_dataset, left_ds_col_name
        )

        return pd.merge(
            left=left_dataset,
            right=right_dataset,
            left_on=[left_ds_col_name],
            right_on=[column_with_values],
            how="outer",
            suffixes=("", f".{right_dataset_domain_name}"),
        )

    @staticmethod
    def merge_relationship_datasets(
        left_dataset: pd.DataFrame,
        left_dataset_match_keys: List[str],
        right_dataset: pd.DataFrame,
        right_dataset_match_keys: List[str],
        right_dataset_domain: dict,
    ) -> pd.DataFrame:
        result = DataProcessor.filter_dataset_by_match_keys_of_other_dataset(
            left_dataset,
            left_dataset_match_keys,
            right_dataset,
            right_dataset_match_keys,
        )
        result = DataProcessor.filter_parent_dataset_by_supp_dataset(
            result,
            right_dataset,
            **right_dataset_domain["relationship_columns"],
        )
        result = result.reset_index(drop=True)
        result = DataProcessor.merge_datasets_on_relationship_columns(
            left_dataset=result,
            right_dataset=right_dataset,
            right_dataset_domain_name=right_dataset_domain.get("domain_name"),
            **right_dataset_domain["relationship_columns"],
        )
        return result

    @staticmethod
    def merge_sdtm_datasets(
        left_dataset: pd.DataFrame,
        right_dataset: pd.DataFrame,
        left_dataset_match_keys: List[str],
        right_dataset_match_keys: List[str],
        right_dataset_domain_name: str,
    ) -> pd.DataFrame:
        result = pd.merge(
            left_dataset,
            right_dataset,
            left_on=left_dataset_match_keys,
            right_on=right_dataset_match_keys,
            suffixes=("", f".{right_dataset_domain_name}"),
        )
        return result

    @staticmethod
    def cast_numeric_cols_to_same_data_type(
        left_dataset: pd.DataFrame,
        left_dataset_column: str,
        right_dataset: pd.DataFrame,
        right_dataset_column: str,
    ):
        """
        Casts given columns to one data type (float)
        ONLY if they are numeric.
        Before casting, the method ensures that both of the
        columns hold numeric values and performs conversion
        only if both of them are.

        Example 1:
            left_dataset[left_dataset_column] = [1, 2, 3, 4, ]
            right_dataset[right_dataset_column] = ["1.0", "2.0", "3.0", "4.0", ]

        Example 2:
            left_dataset[left_dataset_column] = ["1", "2", "3", "4", ]
            right_dataset[right_dataset_column] = ["1.0", "2.0", "3.0", "4.0", ]

        Example 3:
            left_dataset[left_dataset_column] = ["1", "2", "3", "4", ]
            right_dataset[right_dataset_column] = [1, 2, 3, 4, ]
        """
        # check if both columns are numeric
        left_is_numeric: bool = DataProcessor.column_contains_numeric(
            left_dataset[left_dataset_column]
        )
        right_is_numeric: bool = DataProcessor.column_contains_numeric(
            right_dataset[right_dataset_column]
        )
        if left_is_numeric and right_is_numeric:
            # convert to float
            right_dataset[right_dataset_column] = right_dataset[
                right_dataset_column
            ].astype(float)
            left_dataset[left_dataset_column] = left_dataset[
                left_dataset_column
            ].astype(float)

    @staticmethod
    def column_contains_numeric(column: pd.Series) -> bool:
        if not pd.api.types.is_numeric_dtype(column):
            return column.str.replace(".", "").str.isdigit().all()
        return True

    @staticmethod
    def filter_dataset_columns_by_metadata_and_rule(
        columns: List[str],
        define_metadata: List[dict],
        library_metadata: dict,
        rule: dict,
    ) -> List[str]:
        """
        Leaves only those variables where:
            variable origin type is the same as in rule and
            variable core status is the same as in rule
        """
        targets: List[str] = []
        for column in columns:
            if DataProcessor.column_metadata_equal_to_define_and_library(
                column, define_metadata, library_metadata, rule
            ):
                targets.append(column)
        return targets

    @staticmethod
    def column_metadata_equal_to_define_and_library(
        column: str,
        define_metadata: List[dict],
        library_metadata: dict,
        rule: dict,
    ) -> bool:
        define_variable_metadata: Optional[dict] = search_in_list_of_dicts(
            define_metadata, lambda item: item.get("define_variable_name") == column
        )
        if not define_variable_metadata:
            return False
        equal_origin_type: bool = define_variable_metadata[
            "define_variable_origin_type"
        ] == rule.get("variable_origin_type")
        equal_core_status: bool = library_metadata.get(column, {}).get(
            "core"
        ) == rule.get("variable_core_status")
        return equal_core_status and equal_origin_type

    @staticmethod
    def is_dummy_data(data_service: DataServiceInterface) -> bool:
        return isinstance(data_service, DummyDataService)
