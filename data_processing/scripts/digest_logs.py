# This script can be used to digest a set of logs from different processing scripts (currently supported: process_sample logs and process_library logs) for a given donor,seq_run pair. Various actions can be requested, for example: print noteworthy log lines, get a digest in csv, etc.
# - For sample processing logs, run as follows:
#   python digest_logs.py <action> sample <donor_id> <seq_run> <logs_location> <version> <working_dir> <s3_access_file>
# - For library processing logs, run as follows:
#   python digest_logs.py <action> library <donor_id> <seq_run> <logs_location> <version> <working_dir> <s3_access_file>

import sys
import os
import traceback
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
import numpy as np
import pandas as pd
import csv

from parse import *
import logging
import io
import re

import utils
from logger import RichLogger, not_found_sign

logging.getLogger('parse').setLevel(logging.WARNING)

class BaseDigestClass(ABC):
    def __init__(self, args: List[str]):
        self._ingest_and_sanity_check_input(args)
        self.downloaded_from_aws = self.logs_location == "aws"

        # set aws credentials
        if self.logs_location == "aws":
            utils.set_access_keys(self.s3_access_file)

    def _ingest_and_sanity_check_input(self, args):
        assert(len(args) == 10)
        self.donor_id = args[3]
        self.seq_run = args[4]
        self.logs_location = args[5] # must be either "aws" or the absolute path to the logs location on the local disk
        self.version = args[6] # must be either the version to use (e.g. "v1") or the latest version for each sample ("latest") - "latest" can only be used if logs_location is "aws"
        self.version_ir = args[7] # must be either the version to use (e.g. "v1") ignored if version is "latest" - "latest" can only be used if logs_location is "aws"
        self.working_dir = args[8] # must be either "" or the absolute path to the local directory where we will place the logs downloaded from aws - only used if logs_location is "aws"
        self.s3_access_file = args[9] # must be either "" or the absolute path to the aws credentials file - only used if logs_location is "aws"

        assert(self.version != "latest" or self.logs_location == "aws")
        assert(self.logs_location == "aws" or os.path.isdir(self.logs_location))
        assert(self.working_dir == "" if self.logs_location != "aws" else os.path.isdir(self.working_dir))
        assert(self.s3_access_file == "" if self.logs_location != "aws" else os.path.isfile(self.s3_access_file))

        if self.version == "latest":
            logger = RichLogger()
            logger.add_to_log("*** Be wary of using the \"latest\" option as it can hide failures. For example if you expect the latest to be N and some library failed, we would grab the latest version that succeeded (<N) and report success. If you know the version you expect, provide it explicitly. ***", level="warning")

    def _get_all_samples(self) -> pd.DataFrame:
        samples = utils.read_immune_aging_sheet("Samples", quiet=True)
        indices = samples["Donor ID"] == self.donor_id
        return samples[indices]

    @abstractmethod
    def _get_object_ids(self):
        """
        Gathers the full set of object id's we are interested in. These id's can then be used to get the corresponding
        log file name (e.g. using ``_get_log_file_name``) to download logs from.
        """
        pass

    @abstractmethod
    def _get_object_prefix(self, object_id: str):
        pass
        
    @abstractmethod
    def _get_log_file_name(self, object_id: str, version: str):
        pass

    @abstractmethod
    def _get_aws_dir_name(self):
        pass

    @staticmethod
    def _remove_logs(logs_dir: str):
        if os.path.isdir(logs_dir):
            os.system("rm {}/*".format(logs_dir))

    @staticmethod
    def _is_alertable_log_line(line: str) -> bool:
        return BaseDigestClass._is_failure_line(line) or BaseDigestClass._is_warning_line(line)

    @staticmethod
    def _is_failure_line(line: str) -> bool:
        return "ERROR" in line or "CRITICAL" in line or "NOT FOUND" in line

    @staticmethod
    def _is_warning_line(line: str) -> bool:
        return "WARNING" in line

    def _get_log_lines(self) -> Dict[str, List]:
        files_to_lines = {}
        logger = RichLogger()
        try:
            object_ids = self._get_object_ids()
            object_versions = []

            # if the logs location is aws, download all log files to the local working directory
            if self.logs_location == "aws":
                # set this first so that if we hit an exception mid-way through the loop below
                # we can still attempt to clean up the downloaded logs
                self.logs_location = self.working_dir
                for object_id in object_ids:
                    prefix = self._get_object_prefix(object_id)
                    aws_dir_name = self._get_aws_dir_name()
                    # find the latest version if needed
                    if self.version == "latest":
                        latest_version = -1
                        ls_cmd = "aws s3 ls s3://immuneaging/{}/{} --recursive".format(aws_dir_name, prefix)
                        ls  = os.popen(ls_cmd).read()
                        if len(ls) != 0:
                            filenames = ls.split("\n")
                            for filename in filenames:
                                # search for patterns of .vX.log. If there is a match, group
                                # one is ".v" and group 2 is "X" (X can be any integer >0)
                                m = re.search("(\.v)(\d+)\.log$", filename)
                                if bool(m):
                                    version = int(m[2])
                                    if latest_version < version:
                                        latest_version = version
                        # will be v-1 if we could not find any log file above - this will cause
                        # the code further below to fail to find the file and emit an error message
                        version = "v" + str(latest_version)
                    else:
                        if "GEX" in prefix:
                            version = self.version
                        else:
                            version = self.version_ir
                    object_versions.append(version)
                    filename = self._get_log_file_name(object_id, version)
                    sync_cmd = 'aws s3 sync --no-progress s3://immuneaging/{}/{}/{} {} --exclude "*" --include {}'.format(aws_dir_name, prefix, version, self.working_dir, filename)
                    logger.add_to_log("syncing {}...".format(filename))
                    logger.add_to_log("sync_cmd: {}".format(sync_cmd))
                    resp = os.popen(sync_cmd).read()
                    if len(resp) == 0:
                        logger.add_to_log("empty response from aws.\n", level="error")
                    else:
                        logger.add_to_log("aws response: {}\n".format(resp))
            else:
                # object_versions = [self.version] * len(object_ids)
                object_versions = []
                versions = [f"v{num}" for num in range(1,9)]
                for object_id in object_ids:
                    found = False
                    for v in versions:
                        filename = self._get_log_file_name(object_id, v)
                        filepath = os.path.join(self.logs_location, filename)
                        if os.path.isfile(filepath):
                            object_versions.append(v)
                            found = True
                            break
                    if found is False:
                        object_versions.append("-1")

            # for each object id, get all its log lines and add it to the dict
            for elem in zip(object_ids, object_versions):
                filename = self._get_log_file_name(elem[0], elem[1])
                filepath = os.path.join(self.logs_location, filename)
                if not os.path.isfile(filepath):
                    logger.add_to_log("File not found: {}. Skipping.".format(filepath), level="error")
                    files_to_lines[filepath] = ["NOT FOUND {} No logs were found. Either processing failed or logs are unavailable.\n".format(not_found_sign)]
                    continue
                lines = []
                with open(filepath, 'r') as f:
                    lines = f.readlines()
                for line in lines:
                    if filepath not in files_to_lines:
                        files_to_lines[filepath] = [line]
                    else:
                        files_to_lines[filepath].append(line)

            # clean up the logs we downloaded from aws if any
            if self.downloaded_from_aws:
                self._remove_logs(self.logs_location)
        except Exception:
            logger.add_to_log("Execution failed with the following error:\n{}".format(traceback.format_exc()), "critical")
            # clean up the logs we downloaded from aws if any
            if self.downloaded_from_aws:
                self._remove_logs(self.logs_location)
            sys.exit()
        return files_to_lines

    def print_digest(self, log_criterion_override=None):
        logger = RichLogger()
        try:
            files_to_lines = self._get_log_lines()
            
            # for each file, parse its logs and report any noteworthy log events
            log_lines_to_print = {}
            for filepath,lines in files_to_lines.items():
                for line in lines:
                    do_log = self._is_alertable_log_line(line) if log_criterion_override is None else log_criterion_override(line)
                    if do_log:
                        if filepath not in log_lines_to_print:
                            log_lines_to_print[filepath] = [line]
                        else:
                            log_lines_to_print[filepath].append(line)

            # print digested log lines
            if len(log_lines_to_print) == 0:
                logger.add_to_log("No relevant log lines were found.", "info")
            else:
                logger.add_to_log("Found the following relevant log lines.", "info")
                first_item = True
                for key,value in log_lines_to_print.items():
                    if not first_item:
                        # draw a separator line between files
                        utils.draw_separator_line()
                    file_name = key.split("/")[-1]
                    print(file_name + ":\n")
                    for line in value:
                        print("\t" + line)
                    first_item = False
        except Exception:
            logger.add_to_log("Execution failed with the following error:\n{}".format(traceback.format_exc()), "critical")
            sys.exit()

    @abstractmethod
    def get_digest_csv(self):
        pass

class DigestSampleProcessingLogs(BaseDigestClass):
    def __init__(self, args: List[str]):
        super().__init__(args)

    def _get_object_ids(self):
        samples_df = self._get_all_samples()
        return samples_df["Sample_ID"]

    def _get_object_prefix(self, object_id: str):
        return "{}_GEX".format(object_id)        

    def _get_log_file_name(self, object_id: str, version: str):
        prefix = self._get_object_prefix(object_id)
        return "process_sample.{}.{}.log".format(prefix, version)

    def _get_object_id(self, log_file_name: str):
        # log file name is process_sample.{prefix}.{version}.log where prefix is given by _get_object_prefix
        prefix = log_file_name.split(".")[1]
        return prefix.split("_")[0]

    def _get_aws_dir_name(self):
        return "processed_samples"

    def get_digest_csv(self):
        logger = RichLogger()
        try:
            files_to_lines = self._get_log_lines()

            # define the csv digest headers
            CSV_HEADER_SAMPLE_ID: str = "Sample ID"
            CSV_HEADER_CELL_COUNT: str = "# Cells"
            CSV_HEADER_FAILED: str = "Failed?"
            CSV_HEADER_WARNING: str = "Warning?"
            CSV_HEADER_FAILURE_REASON: str = "Failure Reason"
            CSV_HEADER_WARNING_REASON: str = "Warning Reason"
            CSV_HEADER_DOUBLETS: str = "% doublets"
            CSV_HEADER_AMBIENT_RNA: str = "% ambient RNA"
            CSV_HEADER_VDJ: str = "% vdj genes"
            CSV_HEADER_RBC: str = "% RBC"
            CSV_HEADER_LAST_PROCESSED: str = "Last Processed"

            def parse_line(line: str, formatted_str: str, formatted_str_index: int, csv_header: str, csv_row: Dict) -> bool:
                parsed = search(formatted_str, line)
                if parsed:
                    csv_row[csv_header] = parsed[formatted_str_index]
                    return True
                return False

            # for each file, parse its logs and add digest info to csv
            csv_rows = []
            for filepath,lines in files_to_lines.items():
                csv_row = {
                    CSV_HEADER_SAMPLE_ID: self._get_object_id(filepath),
                    CSV_HEADER_CELL_COUNT: 0,
                    CSV_HEADER_FAILED: "No",
                    CSV_HEADER_WARNING: "No",
                    CSV_HEADER_FAILURE_REASON: "",
                    CSV_HEADER_WARNING_REASON: "",
                    CSV_HEADER_DOUBLETS: -1,
                    CSV_HEADER_AMBIENT_RNA: -1,
                    CSV_HEADER_VDJ: -1,
                    CSV_HEADER_RBC: -1,
                    CSV_HEADER_LAST_PROCESSED: "",
                }
                for line in lines:
                    # cell count
                    parse_line(line, utils.QC_STRING_COUNTS, 0, CSV_HEADER_CELL_COUNT, csv_row)
                    # failures and warnings
                    if self._is_failure_line(line):
                        csv_row[CSV_HEADER_FAILED] = "Yes"
                        stripped_line = line.strip('"').strip()
                        if csv_row[CSV_HEADER_FAILURE_REASON] == "": # first one
                            csv_row[CSV_HEADER_FAILURE_REASON] += stripped_line
                        else:
                            csv_row[CSV_HEADER_FAILURE_REASON] += " --- " + stripped_line
                    if self._is_warning_line(line):
                        csv_row[CSV_HEADER_WARNING] = "Yes"
                        stripped_line = line.strip('"').strip()
                        if csv_row[CSV_HEADER_WARNING_REASON] == "": # first one
                            csv_row[CSV_HEADER_WARNING_REASON] += stripped_line
                        else:
                            csv_row[CSV_HEADER_WARNING_REASON] += " --- " + stripped_line
                    # doublets
                    parse_line(line, utils.QC_STRING_DOUBLETS, 1, CSV_HEADER_DOUBLETS, csv_row)
                    # ambient rna
                    parse_line(line, utils.QC_STRING_AMBIENT_RNA, 1, CSV_HEADER_AMBIENT_RNA, csv_row)
                    # vdj genes
                    parse_line(line, utils.QC_STRING_VDJ, 1, CSV_HEADER_VDJ, csv_row)
                    # red blood cells
                    parse_line(line, utils.QC_STRING_RBC, 1, CSV_HEADER_RBC, csv_row)
                    # last processed
                    # the "(" acts as a delimiter, to avoid reading more than needed (since we don't currently log a period after the time)
                    parsed = parse_line(line, utils.QC_STRING_START_TIME + " (", 0, CSV_HEADER_LAST_PROCESSED, csv_row)
                    if parsed:
                        csv_row[CSV_HEADER_LAST_PROCESSED] = utils.get_date_from_time(csv_row[CSV_HEADER_LAST_PROCESSED])
                csv_rows.append(csv_row)

            # write the csv
            csv_file = io.StringIO()
            field_names = [
                CSV_HEADER_SAMPLE_ID,
                CSV_HEADER_CELL_COUNT,
                CSV_HEADER_FAILED,
                CSV_HEADER_WARNING,
                CSV_HEADER_FAILURE_REASON,
                CSV_HEADER_WARNING_REASON,
                CSV_HEADER_DOUBLETS,
                CSV_HEADER_AMBIENT_RNA,
                CSV_HEADER_VDJ,
                CSV_HEADER_RBC,
                CSV_HEADER_LAST_PROCESSED,
            ]
            writer = csv.DictWriter(csv_file, fieldnames=field_names)
            writer.writeheader()
            writer.writerows(csv_rows)
            print(csv_file.getvalue())
            csv_file.close()
        except Exception:
            logger.add_to_log("Execution failed with the following error:\n{}".format(traceback.format_exc()), "critical")
            sys.exit()

class DigestLibraryProcessingLogs(BaseDigestClass):
    def __init__(self, args: List[str]):
        super().__init__(args)

    def _get_object_id(self, log_file_name: str):
        prefix = log_file_name.split(".")[1]
        return prefix.split("_")[0]

    def _get_object_ids(self):
        object_ids = set()
        samples_df = self._get_all_samples()
        for library_type in ["GEX", "BCR", "TCR"]:
            for i in samples_df[library_type + " lib"]:
                if i is np.nan:
                    continue
                for j in i.split(","):
                    object_ids.add("{}_{}".format(library_type, j))
        return object_ids

    def _get_object_prefix(self, object_id: str):
        return "{}_{}_{}".format(self.donor_id, self.seq_run, object_id)

    def _get_log_file_name(self, object_id: str, version: str):
        prefix = self._get_object_prefix(object_id)
        return "process_library.{}.{}.log".format(prefix, version)

    def _get_aws_dir_name(self):
        return "processed_libraries"

    def get_digest_csv(self):
        raise NotImplementedError

    def get_lib_metrics_csv(self, csv_file: str, lib_types: Optional[List[str]] = None):
        logger = RichLogger()
        try:
            files_to_lines = self._get_log_lines()

            # define the csv digest headers
            CSV_HEADER_LIBRARY_ID: str = "lib_id"
            CSV_HEADER_CELL_COUNT_BEFORE_QC: str = "# Cells Before QC"
            CSV_HEADER_CELL_COUNT_AFTER_QC: str = "# Cells After QC"
            CORRESPONDING_GEX_LIB: str = "corresponding_gex_lib"
            DONOR_ID: str = "donor_id"
            LIB_TYPE: str = "lib_type"

            def parse_line(line: str, formatted_str: str, formatted_str_index: int, csv_header: str, csv_row: Dict) -> bool:
                parsed = search(formatted_str, line)
                if parsed:
                    csv_row[csv_header] = parsed[formatted_str_index]
                    return True
                return False

            # for each file, parse its logs and add digest info to csv
            csv_rows = []
            for filepath,lines in files_to_lines.items():
                lib_id = filepath.split(".")[1].split("_")[-1]
                lib_type = filepath.split(".")[1].split("_")[-2] # e.g. GEX, BCR, TCR
                if lib_types is not None and lib_type not in lib_types:
                    continue
                csv_row = {
                    CSV_HEADER_LIBRARY_ID: lib_id,
                    CSV_HEADER_CELL_COUNT_BEFORE_QC: 0,
                    CSV_HEADER_CELL_COUNT_AFTER_QC: 0,
                    CORRESPONDING_GEX_LIB: "?",
                    DONOR_ID: "?",
                    LIB_TYPE: lib_type,
                }
                for line in lines:
                    FORMATTED_STRING_COUNTS_BEGIN = "Started with a total of {} cells"
                    FORMATTED_STRING_COUNTS_END = "Final number of cells: {}."
                    if lib_type == "GEX":
                        FORMATTED_STRING_COUNTS_END = "Final number of cells: {},"
                    FORMATTED_STRING_CORRESPONDING_GEX_LIB = ", 'corresponding_gex_lib': '{}',"
                    FORMATTED_STRING_DONOR_ID = ", 'donor': '{}',"
                    parse_line(line, FORMATTED_STRING_COUNTS_BEGIN, 0, CSV_HEADER_CELL_COUNT_BEFORE_QC, csv_row)
                    parse_line(line, FORMATTED_STRING_COUNTS_END, 0, CSV_HEADER_CELL_COUNT_AFTER_QC, csv_row)
                    if lib_type != "GEX":
                        parse_line(line, FORMATTED_STRING_CORRESPONDING_GEX_LIB, 0, CORRESPONDING_GEX_LIB, csv_row)
                    parse_line(line, FORMATTED_STRING_DONOR_ID, 0, DONOR_ID, csv_row)
                csv_rows.append(csv_row)

            # write the csv
            df = pd.DataFrame.from_records(csv_rows)
            df.to_csv(csv_file, mode="a", header=not os.path.isfile(csv_file), index=None) 
        except Exception:
            logger.add_to_log("Execution failed with the following error:\n{}".format(traceback.format_exc()), "critical")
            sys.exit()


if __name__ == "__main__":
    process_type = sys.argv[2] # must be one of "sample" or "library"
    action = sys.argv[1] # must be one of "print_digest" or "get_csv"
    assert(process_type in ["sample", "library"])
    assert(action in ["print_digest", "get_csv"])
    if process_type == "sample":
        digest_class = DigestSampleProcessingLogs(sys.argv)
    else:
        digest_class = DigestLibraryProcessingLogs(sys.argv)
    if action == "print_digest":
        digest_class.print_digest()
    else:
        digest_class.get_digest_csv()