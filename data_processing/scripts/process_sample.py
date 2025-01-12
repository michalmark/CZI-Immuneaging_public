#########################################################
###### INITIALIZATIONS AND PREPARATIONS BEGIN HERE ######
#########################################################

import sys
import logging
import os
import json
import scanpy as sc
import numpy as np
import pandas as pd
from scipy.sparse.csr import csr_matrix
import scipy.sparse as sparse
import scvi
import hashlib
import celltypist
import urllib.request
import traceback
import scirpy as ir
import scrublet
from typing import Optional

logging.getLogger('numba').setLevel(logging.WARNING)

process_sample_script = sys.argv[0]
configs_file = sys.argv[1]

sc.settings.verbosity = 3   # verbosity: errors (0), warnings (1), info (2), hints (3)

with open(configs_file) as f: 
    data = f.read()	

configs = json.loads(data)
sandbox_mode = configs["sandbox_mode"] == "True"
output_destination = configs["output_destination"]
donor = configs["donor"]
seq_run = configs["seq_run"]
sample_id = configs["sample_id"]

sys.path.append(configs["code_path"])

from utils import *
from vdj_utils import *
from logger import SimpleLogger
init_scvi_settings()

# config changes only to these fields will not initialize a new configs version
VARIABLE_CONFIG_KEYS = ["data_owner","s3_access_file","code_path","output_destination"]

# a map between fields in the Donors sheet of the Google Spreadsheet to metadata fields
DONORS_FIELDS = {"Donor ID": "donor_id",
    "Site (UK/ NY)": "site",
    "DCD/DBD": "DCD/DBD",
    "Age (years)": "age",
    "Sex": "sex",
    "ethnicity/race": "ethnicity/race",
    "cause of death": "death_cause",
    "mech of injury": "mech_injury",
    "height (cm)": "height",
    "BMI (kg/m^2)": "BMI",
    "lipase level": "lipase_level",
    "blood sugar (mg/dL)": "blood_sugar",
    "Period of time in relation to smoking": "smoking_period",
    "smoker (pack-years)": "smoking_packs_year",
    "EBV status": "EBV",
    "CMV status": "CMV"}

# a map between fields in the Samples sheet of the Google Spreadsheet to metadata fields
SAMPLES_FIELDS = {"Sample_ID": "sample_id",
    "Seq run": "seq_run",
    "Fresh/frozen": "Fresh/frozen",
    "Cell type": "sample_cell_type",
    "Sorting": "sorting",
    "Stimulation": "stimulation",
    "Free text": "Notes"}

# apply the aws credentials to allow access though aws cli; make sure the user is authorized to run in non-sandbox mode if applicable
s3_dict = set_access_keys(configs["s3_access_file"], return_dict = True)
assert sandbox_mode or hashlib.md5(bytes(s3_dict["AWS_SECRET_ACCESS_KEY"], 'utf-8')).hexdigest() in AUTHORIZED_EXECUTERS, "You are not authorized to run this script in a non sanbox mode; please set sandbox_mode to True"
set_access_keys(configs["s3_access_file"])

# create a new directory for the data and outputs
data_dir = os.path.join(output_destination, "_".join([donor, seq_run]))
os.system("mkdir -p " + data_dir)

# check for previous versions of the processed sample
prefix = "{}_{}".format(sample_id, "GEX") # hard code "GEX" in the prefix in order to keep consistent with what we already have on AWS and not have to rename everything
s3_path = "s3://immuneaging/processed_samples/" + prefix
is_new_version, version = get_configs_status(configs, s3_path, "process_sample.configs." + prefix,
    VARIABLE_CONFIG_KEYS, data_dir)
output_configs_file = "process_sample.configs.{}.{}.txt".format(prefix,version)

# set up logger
logger_file = "process_sample.{}.{}.log".format(prefix,version)
logger_file_path = os.path.join(data_dir, logger_file)
if os.path.isfile(logger_file_path):
    os.remove(logger_file_path)

logger = SimpleLogger(filename = logger_file_path)
logger.add_to_log("Running process_sample.py...")
logger.add_to_log(QC_STRING_START_TIME.format(get_current_time()))
with open(process_sample_script, "r") as f:
    logger.add_to_log("process_sample.py md5 checksum: {}\n".format(hashlib.md5(bytes(f.read(), 'utf-8')).hexdigest()))

logger.add_to_log("using the following configurations:\n{}".format(str(configs)))
logger.add_to_log("Configs version: " + version)
logger.add_to_log("New configs version: " + str(is_new_version))

h5ad_file = "{}.processed.{}.h5ad".format(prefix, version)
if is_new_version:
    cp_cmd = "cp {} {}".format(configs_file, os.path.join(data_dir,output_configs_file))
    os.system(cp_cmd)
    if not sandbox_mode:
        logger.add_to_log("Uploading new configs version to S3...")
        sync_cmd = 'aws s3 sync --no-progress {} s3://immuneaging/processed_samples/{}/{}/ --exclude "*" --include {}'.format(
            data_dir, prefix, version, output_configs_file)
        logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))
else:
    logger.add_to_log("Checking if h5ad file already exists on S3...")
    h5ad_file_exists = False
    logger_file_exists = False
    ls_cmd = "aws s3 ls s3://immuneaging/processed_samples/{}/{} --recursive".format(prefix,version)
    files = os.popen(ls_cmd).read()
    logger.add_to_log("aws response: {}\n".format(files))
    for f in files.rstrip().split('\n'):
        if f.split('/')[-1] == h5ad_file:
            h5ad_file_exists = True
        if f.split('/')[-1] == logger_file:
            logger_file_exists = True
    if h5ad_file_exists and logger_file_exists:
        logger.add_to_log("The following h5ad file is already on S3: {}\nTerminating execution.".format(h5ad_file))
        sys.exit()
    if h5ad_file_exists and not logger_file_exists:
        logger.add_to_log("The following h5ad file is already on S3: {}\nhowever, log file is missing on S3; proceeding with execution.".format(h5ad_file))
    if not h5ad_file_exists:
        logger.add_to_log("The following h5ad file does not exist on S3: {}".format(h5ad_file))

library_ids = configs["library_ids"].split(',')
assert "library_types" in configs or "library_type" in configs
if "library_types" in configs:
    library_types = configs["library_types"].split(',')
else:
    library_types = [configs["library_type"]] * len(library_ids)
library_versions = configs["processed_library_configs_version"].split(',')
if ("processed_libraries_dir" in configs) and (len(configs.get("processed_libraries_dir")) > 0):
    processed_libraries_dir = configs["processed_libraries_dir"]
    logger.add_to_log("Copying h5ad files of processed libraries from {}...".format(processed_libraries_dir))
    cp_cmd = "cp -r {}/ {}".format(processed_libraries_dir.rstrip("/"), data_dir)
    os.system(cp_cmd)
else:
    logger.add_to_log("Downloading h5ad files of processed libraries from S3...")
    logger.add_to_log("*** Note: This can take some time. If you already have the processed libraries, you can halt this process and provide processed_libraries_dir in the config file in order to use your existing h5ad files. ***")   
    for j in range(len(library_ids)):
        library_id = library_ids[j]
        library_type = library_types[j]
        library_version = library_versions[j]
        lib_h5ad_file = "{}_{}_{}_{}.processed.{}.h5ad".format(donor, seq_run,
            library_type, library_id, library_version)
        sync_cmd = 'aws s3 sync --no-progress s3://immuneaging/processed_libraries/{}_{}_{}_{}/{}/ {} --exclude "*" --include {}'.format(
            donor, seq_run, library_type, library_id, library_version, data_dir, lib_h5ad_file)
        logger.add_to_log("syncing {}...".format(lib_h5ad_file))
        logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))

summary = ["\n{0}\nExecution summary\n{0}".format("="*25)]

logger.add_to_log("Downloading the Donors sheet from the Google spreadsheets...")
donors = read_immune_aging_sheet("Donors")
logger.add_to_log("Downloading the Samples sheet from the Google spreadsheets...")
samples = read_immune_aging_sheet("Samples")

############################################
###### SAMPLE PROCESSING BEGINS HERE #######
############################################

logger.add_to_log("Reading h5ad files of processed libraries for GEX libs...")
adata_dict = {}
initial_n_obs = 0
sub_genes = set()
library_ids_gex = []
for j in range(len(library_ids)):
    if library_types[j] != "GEX":
        continue
    library_id = library_ids[j]
    library_type = library_types[j]
    library_version = library_versions[j]
    lib_h5ad_file = os.path.join(data_dir, "{}_{}_{}_{}.processed.{}.h5ad".format(donor, seq_run,
        library_type, library_id, library_version))
    if not os.path.isfile(lib_h5ad_file):
        # This could either be an error or a legitimate case of missing library (for example if say all cells from that
        # lib were filtered out during lib processing). We want to know about it in either case but this is not the place
        # for it. In both of the aforementioned cases, we'd get to know about it at the outcome of lib processing.
        # If this a known case of poor quality, don't log a warning.
        poor_quality_libs_df = read_csv_from_aws(data_dir, "s3://immuneaging/cell_filtering/", "poor_quality_libs.csv", logger)
        level = "debug" if donor + "_" + library_id in poor_quality_libs_df.columns else "warning"
        logger.add_to_log("Library {} not found. Skipping...".format(library_id), level=level)
        continue
    adata_dict[library_id] = sc.read_h5ad(lib_h5ad_file)
    adata_dict[library_id].obs["library_id"] = library_id
    if "Classification" in adata_dict[library_id].obs.columns:
        adata_dict[library_id] = adata_dict[library_id][adata_dict[library_id].obs["Classification"] == sample_id].copy()
        if "min_cells_per_library" in configs and configs["min_cells_per_library"] > adata_dict[library_id].n_obs:
            # do not consider cells from this library
            msg = "Cells from library {} were not included - there are {} cells from the sample, however, min_cells_per_library was set to {}.".format(
                library_id,adata_dict[library_id].n_obs,configs["min_cells_per_library"])
            logger.add_to_log(msg, "warning")
            del adata_dict[library_id]
            continue
    # filter out libs that have a median gene per cell that is lower than the set threshold, if any
    lib_medgpc_value = adata_dict[library_id].uns["lib_metrics"][CELLRANGER_METRICS.MEDIAN_GENES_PER_CELL]
    lib_medgpc = int(lib_medgpc_value.replace(",", "")) if type(lib_medgpc_value) == str else lib_medgpc_value
    if "min_MedGPC_per_library" in configs and configs["min_MedGPC_per_library"] > lib_medgpc:
            # do not consider cells from this library
            msg = "Cells from library {} were not included - library's median gene per cell value is {}, however min_MedGPC_per_library was set to {}.".format(library_id, lib_medgpc, configs["min_MedGPC_per_library"])
            logger.add_to_log(msg, "warning")
            del adata_dict[library_id]
            continue
    # filter out libs that have a median UMI per cell that is lower than the set threshold, if any
    lib_medupc_value = adata_dict[library_id].uns["lib_metrics"][CELLRANGER_METRICS.MEDIAN_UMI_COUNTS_PER_CELL]
    lib_medupc = int(lib_medupc_value.replace(",", "")) if type(lib_medupc_value) == str else lib_medupc_value
    if "min_MedUPC_per_library" in configs and configs["min_MedUPC_per_library"] > lib_medupc:
            # do not consider cells from this library
            msg = "Cells from library {} were not included - library's median UMI per cell value is {}, however min_MedUPC_per_library was set to {}.".format(library_id, lib_medupc, configs["min_MedUPC_per_library"])
            logger.add_to_log(msg, "warning")
            del adata_dict[library_id]
            continue
    library_ids_gex.append(library_id)
    sub_genes_j = np.logical_and(sc.pp.filter_genes(adata_dict[library_id], min_cells=configs["solo_filter_genes_min_cells"], inplace=False)[0], (adata_dict[library_id].var["feature_types"] == "Gene Expression").values)
    sub_genes_j = adata_dict[library_id].var.index[sub_genes_j]
    initial_n_obs += adata_dict[library_id].n_obs
    if len(sub_genes)==0:
        sub_genes = sub_genes_j
    else:
        sub_genes = np.intersect1d(sub_genes,sub_genes_j)

if len(library_ids_gex)==0:
    logger.add_to_log("No cells passed the filtering steps. Terminating execution.", "error")
    logging.shutdown()
    if not sandbox_mode:
        # Uploading log file to S3...
        sync_cmd = 'aws s3 sync --no-progress {} s3://immuneaging/processed_samples/{}/{}/ --exclude "*" --include {}'.format(
            data_dir, prefix, version, logger_file)
        os.system(sync_cmd)
    sys.exit()

logger.add_to_log("Concatenating all cells of sample {} from available GEX libraries...".format(sample_id))
adata = adata_dict[library_ids_gex[0]]
if len(library_ids_gex) > 1:
    adata = adata.concatenate([adata_dict[library_ids_gex[j]] for j in range(1,len(library_ids_gex))], join="outer")
def build_adata_from_ir_libs(lib_type: str, library_ids_ir: List[str]) -> Optional[AnnData]:
    assert lib_type in ["BCR", "TCR"]
    logger.add_to_log("Reading h5ad files of processed libraries for {} libs...".format(lib_type))
    adata_dict = {}
    for j in range(len(library_ids)):
        if library_types[j] != lib_type:
            continue
        library_id = library_ids[j]
        library_type = library_types[j]
        library_version = library_versions[j]
        # find the corresponding gex lib id and see if it is in library_ids_gex. If not,
        # we should exclude it here as well
        ir_libs = []
        gex_libs = []
        for elem in zip(library_ids, library_types):
            if elem[1] == lib_type:
                ir_libs.append(elem[0])
            elif elem[1] == "GEX":
                gex_libs.append(elem[0])
        assert len(ir_libs) == len(gex_libs) and len(ir_libs) != 0
        idx = ir_libs.index(library_id)
        gex_lib = gex_libs[idx]
        if gex_lib not in library_ids_gex:
            logger.add_to_log("No GEX counterpart found for library id {} of type {}. Ignoring it...".format(library_id, lib_type), level="warning")
            continue
        lib_h5ad_file = os.path.join(data_dir, "{}_{}_{}_{}.processed.{}.h5ad".format(donor, seq_run,
            library_type, library_id, library_version))
        if not os.path.isfile(lib_h5ad_file):
            logger.add_to_log("Failed to find library with id {} of type {}. Moving on...".format(library_id, lib_type), level="warning")
            continue
        adata_dict[library_id] = sc.read_h5ad(lib_h5ad_file)
        adata_dict[library_id].obs["{}-library_id".format(lib_type)] = library_id
        library_ids_ir.append(library_id)

    if len(library_ids_ir) == 0:
        logger.add_to_log("No libraries of type {} were found.".format(lib_type))
        return None

    # Note it's important that we concatenate these libs exactly in this order so that it matches the order in which the corresponding
    # GEX libs where concatenated. This is important because the concat operation appends a -N suffix to all cells where N is the batch
    # id (N=0..len(lib_ids)-1). If we don't do this, cells from IR libs and GEX libs won't match due to mismatching -N suffix.
    logger.add_to_log("Concatenating all cells of sample {} from available {} libraries...".format(sample_id, lib_type))
    adata_to_return = adata_dict[library_ids_ir[0]]
    if len(library_ids_ir) > 1:
        batch_categories = None
        if len(library_ids_ir) < len(library_ids_gex):
            # There can be cases where gex libs A, B, C are present but the IR lib corresponding to B failed. In
            # this case, we should specify a batch_categories sequence to adata.concatenate. Otherwise it would
            # select [0, 1] for the IR libs and [0, 1, 2] for the gex libs and we would have a "1 vs 2" mismatch
            # for lib C
            all_ir_libs = get_vdj_lib_to_gex_lib_mapping(samples=samples)[0 if lib_type == "BCR" else 1] # e.g. {"CZI-BCR-789": "CZI-GEX-456"}
            lib_to_seq = dict(zip(library_ids_gex, range(len(library_ids_gex)))) # e.g. {"CZI-GEX-123": 0, "CZI-GEX-456": 1}
            batch_categories = [str(lib_to_seq[all_ir_libs[l]]) for l in library_ids_ir]
            logger.add_to_log("batch categories: {}".format(batch_categories))
        adata_to_return = adata_to_return.concatenate(
            [adata_dict[library_ids_ir[j]] for j in range(1,len(library_ids_ir))],
            batch_key="temp_batch",
            batch_categories=batch_categories,
        )

        # validate that the assumption above holds, i.e. the batch corresponding to each corresponding_gex_lib is the same
        # for all cells in the adata_ir and is the same as the one in adata
        for j in range(len(library_ids_ir)):
            lib_id = library_ids_ir[j]
            adata_ir_lib_id = adata_to_return[adata_to_return.obs["{}-library_id".format(lib_type)] == lib_id, :].copy()
            ir_batch = adata_ir_lib_id.obs["temp_batch"]
            unique_ir_batch = np.unique(ir_batch)
            assert len(unique_ir_batch) == 1
            # grab the corresponding gex lib value
            batch_suffix = "-{}".format(unique_ir_batch[0])
            corresponding_gex_lib = adata_ir_lib_id.obs.iloc[0].name.split("_")[1][:-len(batch_suffix)]
            adata_lib_id = adata[adata.obs["library_id"] == corresponding_gex_lib, :].copy()
            gex_batch = adata_lib_id.obs["batch"]
            unique_gex_batch = np.unique(gex_batch)
            assert len(unique_gex_batch) == 1
            assert unique_ir_batch[0] == unique_gex_batch[0]

        # we could keep this to know what batch (library) the cells are from, but we'll already have this via
        # the library_id info in adata
        del adata_to_return.obs["temp_batch"]

    return adata_to_return

library_ids_bcr = []
library_ids_tcr = []
adata_bcr = build_adata_from_ir_libs("BCR", library_ids_bcr)
adata_tcr = build_adata_from_ir_libs("TCR", library_ids_tcr)
bcr_cells = 0 if adata_bcr is None else adata_bcr.n_obs
tcr_cells = 0 if adata_tcr is None else adata_tcr.n_obs
logger.add_to_log("Total cells from GEX lib(s): {}, from BCR lib(s): {}, from TCR lib(s): {}".format(adata.n_obs, bcr_cells, tcr_cells))

if adata_bcr is not None and adata_tcr is not None:
    intersection = np.intersect1d(adata_bcr.obs.index, adata_tcr.obs.index)
    intersection_pct = (len(intersection)/(adata_bcr.n_obs + adata_tcr.n_obs)) * 100
    logger.add_to_log("Filtering out cells that have both BCR and TCR...")
    logger.add_to_log("Detected {} cells that have both BCR and TCR ({:.2f}% of total). Unique cell count from BCR+TCR libs: {}".format(len(intersection), intersection_pct, adata_bcr.n_obs + adata_tcr.n_obs))
    
    adata_bcr_new = adata_bcr[~adata_bcr.obs.index.isin(intersection), :].copy()
    adata_tcr_new = adata_tcr[~adata_tcr.obs.index.isin(intersection), :].copy()
    adata.obs['double_ir'] = adata.obs_names.isin(intersection).astype(str)
    
    logger.add_to_log("Concatenating BCR and TCR lib(s)...")
    adata_ir = adata_bcr_new.concatenate(adata_tcr_new, batch_key="temp_batch", index_unique=None)
    del adata_ir.obs["temp_batch"]
elif adata_bcr is not None:
    adata_ir = adata_bcr
elif adata_tcr is not None:
    adata_ir = adata_tcr
else:
    adata_ir = None
if adata_ir is None:
    logger.add_to_log("No anndata with BCR/TCR data")
else:
    # cells that are outside the intersection of adata and adata_ir are either cells that don't have ir data (no bcr or tcr), or are cells that
    # have ir info but no gex. the latter can be due to cellranger miscalling a cell (see https://support.10xgenomics.com/single-cell-vdj/software/pipelines/latest/using/multi#why),
    # or it could be that we didn't sequence those cells for gex as a consequence of the experimental setup - in either case, we are not interested in
    # the ir info for those cells since we are missing gex info for them
    logger.add_to_log("{} cells coming from GEX libs, {} cells coming from BCR+TCR IR libs, {} cells are in the intersection of both.".format(
            len(adata.obs.index),
            len(adata_ir.obs.index),
            len(np.intersect1d(adata.obs.index, adata_ir.obs.index))
        ))
    ir_gex_diff = len(set(adata_ir.obs.index) - set(adata.obs.index))
    ir_gex_diff_pct = (ir_gex_diff/len(adata_ir.obs.index)) * 100
    logger.add_to_log("{} cells coming from BCR+TCR libs have no GEX (mRNA) info (percentage: {:.2f}%)".format(ir_gex_diff, ir_gex_diff_pct))
    logger.add_to_log("Merging IR data from BCR and TCR lib(s) with count data from GEX lib(s)...")
if adata_bcr is not None:
    ir.pp.merge_with_ir(adata, adata_bcr)
if adata_tcr is not None:
    ir.pp.merge_with_ir(adata, adata_tcr)

logger.add_to_log("A total of {} cells and {} genes were found.".format(adata.n_obs, adata.n_vars))
summary.append("Started with a total of {} cells and {} genes coming from {} GEX libraries, {} BCR libraries and {} TCR libraries.".format(
    initial_n_obs, adata.n_vars, len(library_ids_gex), len(library_ids_bcr), len(library_ids_tcr)))

logger.add_to_log("Adding metadata...")
donor_index = donors["Donor ID"] == donor
logger.add_to_log("Adding donor-level metadata...")
for k in DONORS_FIELDS.keys():
    adata.obs[DONORS_FIELDS[k]] = donors[k][donor_index].values[0]

logger.add_to_log("Adding sample-level metadata...")
sample_index = samples["Sample_ID"] == sample_id
for k in SAMPLES_FIELDS.keys():
    adata.obs[SAMPLES_FIELDS[k]] = samples[k][sample_index].values[0]

if "GEX" in library_types:
    adata.obs["GEX_chem"] = samples["GEX chem"][sample_index].values[0]
    adata.obs["HTO_chem"] = samples["HTO chem"][sample_index].values[0]
    adata.obs["ADT_chem"] = samples["CITE chem"][sample_index].values[0]
if "BCR" in library_types:
    adata.obs["BCR_chem"] = samples["BCR chem"][sample_index].values[0]
if "TCR" in library_types:
    adata.obs["TCR_chem"] = samples["TCR chem"][sample_index].values[0]

if adata.n_obs > 0:
    no_cells = False
    logger.add_to_log("Current number of cells: {}.".format(adata.n_obs))
    logger.add_to_log("Current number of genes: {}.".format(adata.n_vars))
else:
    no_cells = True
    logger.add_to_log("Detected no cells. Skipping data processing steps.", "error")
    summary.append("Detected no cells; skipped data processing steps.")

if not no_cells:
    try:
        prot_exp_obsm_key = "protein_expression"
        prot_exp_ctrl_obsm_key = "protein_expression_Ctrl"
        is_cite = prot_exp_obsm_key in adata.obsm
        if is_cite:
            logger.add_to_log("Detected Antibody Capture features.")
            protein_df = adata.obsm[prot_exp_obsm_key].merge(adata.obsm[prot_exp_ctrl_obsm_key], left_index=True, right_index=True, validate="one_to_one")
            if np.median(protein_df.fillna(0).sum(axis=1)) == 0:
                logger.add_to_log("median coverage (total number of protein reads per cell) across cells is 0. Removing protein information from data.", level = "warning")
                is_cite = False
            adata.obs['n_proteins'] = adata.obsm[prot_exp_obsm_key].sum(1)
        else:
            adata.obs['n_proteins'] = 0
        if len(library_ids_gex)>1:
            batch_key = "batch"
        else:
            batch_key = None
        logger.add_to_log("Running decontX for estimating contamination levels from ambient RNA...")
        
        decontx_data_dir = os.path.join(data_dir,"decontx")
        os.system("mkdir -p " + decontx_data_dir)
        raw_counts_file = os.path.join(decontx_data_dir, "{}_raw_counts.npz".format(prefix))
        decontaminated_counts_file = os.path.join(decontx_data_dir, "{}_decontx_decontaminated.npz".format(prefix))
        contamination_levels_file = os.path.join(decontx_data_dir, "{}_decontx_contamination.txt".format(prefix))
        decontx_model_file = os.path.join(decontx_data_dir, "{}_{}_decontx_model.RData".format(prefix, version))
        r_script_file = os.path.join(decontx_data_dir, "{}_decontx_script.R".format(prefix))
        sparse.save_npz(raw_counts_file, adata.X.T)
        if len(library_ids_gex)>1:
            batch_file = os.path.join(decontx_data_dir, "{}_batch.txt".format(prefix))
            pd.DataFrame(adata.obs[batch_key].values.astype(str)).to_csv(batch_file, header=False, index=False)
        else:
            batch_file = None
        # R commands for running and outputing decontx
        l = [
            "library('celda')",
            "library('reticulate')",
            "scipy_sparse <- import('scipy.sparse')",
            "x <- scipy_sparse$load_npz('{}')".format(raw_counts_file),
            "dimnames(x) <- list(NULL,NULL)",
            "batch <- if ('{0}' == 'None') NULL else as.character(read.table('{0}', header=FALSE)$V1)".format(batch_file),
            "res <- decontX(x=x, batch=batch)",
            "write.table(res$contamination, file ='{}',quote = FALSE,row.names = FALSE,col.names = FALSE)".format(contamination_levels_file),
            "scipy_sparse$save_npz('{}', res$decontXcounts)".format(decontaminated_counts_file),
            "decontx_model <- list('estimates'=res$estimates, 'z'= res$z)",
            "save(decontx_model, file='{}')".format(decontx_model_file)
        ]
        with open(r_script_file,'w') as f: 
            f.write("\n".join(l))
        logger.add_to_log("Running the script in {}".format(decontx_data_dir))
        os.system(f"{configs['rscript']} {r_script_file}")
        logger.add_to_log("Adding decontaminated counts and contamination levels to data object...")
        contamination_levels = pd.read_csv(contamination_levels_file, index_col=0, header=None).index
        decontaminated_counts = sparse.load_npz(decontaminated_counts_file).T
        adata.obs["contamination_levels"] = contamination_levels
        adata.layers['decontaminated_counts'] = decontaminated_counts
        rna = adata.copy()
        rna = rna[:,rna.var.index.isin(sub_genes)].copy()
        # remove empty cells after decontaminations
        n_obs_before = rna.n_obs
        rna = rna[rna.layers['decontaminated_counts'].sum(axis=1) >= configs["filter_decontaminated_cells_min_genes"],:].copy()
        n_decon_cells_filtered = n_obs_before-rna.n_obs
        percent_removed = 100*n_decon_cells_filtered/n_obs_before
        level = "warning" if percent_removed > 10 else "info"
        msg = QC_STRING_AMBIENT_RNA.format(n_decon_cells_filtered, percent_removed, configs["filter_decontaminated_cells_min_genes"])
        logger.add_to_log(msg, level=level)
        summary.append(msg)
        # This set of V(D)J genes are expected to express high donor-level variability that is not interesting to us as we want to get a
        # coherent picture across all donors combined. Thus we filter these out prior to HVG selection, but keep them in the data otherwise.
        logger.add_to_log("Filtering out vdj genes...")
        rna = filter_vdj_genes(rna, configs["vdj_genes"], data_dir, logger)
        logger.add_to_log("Detecting highly variable genes...")
        rna.layers["rounded_decontaminated_counts_copy"] = rna.X.copy()
        if configs["highly_variable_genes_flavor"] != "seurat_v3":
            # highly_variable_genes requires log-transformed data in this case
            sc.pp.log1p(rna)
        sc.pp.highly_variable_genes(rna, n_top_genes=configs["n_highly_variable_genes"], subset=True, flavor=configs["highly_variable_genes_flavor"], span = 1.0)
        rna.X = rna.layers["rounded_decontaminated_counts_copy"]
        logger.add_to_log("Predict cell type labels using celltypist...")
        model_urls = configs["celltypist_model_urls"].split(",")
        if configs["rbc_model_url"] != "":
            model_urls.append(configs["rbc_model_url"])
        # run prediction using every specified model (url)
        rbc_model_name = None
        rna_copy = rna.copy()
        # normalize the copied data with a scale of 10000 (which is the scale required by celltypist)
        logger.add_to_log("normalizing data for celltypist...")
        sc.pp.normalize_total(rna_copy, target_sum=10000)
        sc.pp.log1p(rna_copy)
        for i in range(len(model_urls)):
            model_file = model_urls[i].split("/")[-1]
            celltypist_model_name = model_file.split(".")[0]
            model_path = os.path.join(data_dir,model_file)
            # download reference data
            if model_file in os.listdir(data_dir):
                pass
            elif model_urls[i].startswith("s3://"):
                model_folder = model_urls[i][:-len(model_file)] # remove the model_file suffix
                aws_sync(model_folder, data_dir, model_file, logger)
            else:
                urllib.request.urlretrieve(model_urls[i], model_path)
            model = celltypist.models.Model.load(model = model_path)
            if "celltypist_over_clustering" in rna.obs.columns:
                over_clustering = rna.obs["celltypist_over_clustering"]
            else:
                over_clustering = None
            # for some reason celltypist changes the anndata object in a way that then doesn't allow to copy it (which is needed later); a fix is to use a copy of the anndata object.
            predictions = celltypist.annotate(rna_copy, model = model, majority_voting = True, over_clustering=over_clustering)
            # save the index for the RBC model if one exists, since we will need it further below
            if model_file.startswith("RBC_model"):
                rbc_model_name = celltypist_model_name
            logger.add_to_log("Saving celltypist annotations for model {}, model description:\n{}".format(model_file, json.dumps(model.description, indent=2)))
            rna.obs["celltypist_over_clustering"+celltypist_model_name] = predictions.predicted_labels["over_clustering"]
            if "celltypist_over_clustering" not in rna.obs.columns:
                rna.obs["celltypist_over_clustering"] = rna.obs["celltypist_over_clustering"+celltypist_model_name]
            rna.obs["celltypist_majority_voting."+celltypist_model_name] = predictions.predicted_labels["majority_voting"]
            rna.obs["celltypist_predicted_labels."+celltypist_model_name] = predictions.predicted_labels["predicted_labels"]
            rna.obs["celltypist_model."+celltypist_model_name] = model_urls[i]
        # filter out RBC's
        if rbc_model_name:
            n_obs_before = rna.n_obs
            rna.obs['predicted_erythrocyte'] = rna.obs["celltypist_predicted_labels."+rbc_model_name] == "RBC"
            percent_removed = 100*(np.sum(rna.obs['predicted_erythrocyte']))/n_obs_before
            level = "warning" if percent_removed > 20 else "info"
            logger.add_to_log(QC_STRING_RBC.format(np.sum(rna.obs['predicted_erythrocyte']), percent_removed, rna.n_obs), level=level)
        if is_cite:
            # there are known spurious failures with totalVI (such as "invalid parameter loc/scale")
            # so we try a few times then carry on with the rest of the script as we can still mine the
            # rest of the data regardless of CITE info
            retry_count = 4
            try:
                _, totalvi_model_file = run_model(rna, configs, batch_key, prot_exp_obsm_key, "totalvi", prefix, version, data_dir, logger, max_retry_count=retry_count)
            except Exception as err:
                logger.add_to_log("Execution of totalVI failed with the following error (latest) with retry count {}: {}. Moving on...".format(retry_count, err), "warning")
                is_cite = False
        scvi_model, scvi_model_file = run_model(rna, configs, batch_key, None, "scvi", prefix, version, data_dir, logger)
        logger.add_to_log("Running scrublet for detecting doublets...")
        if len(library_ids_gex)>1:
            batches = pd.unique(rna.obs[batch_key])
            logger.add_to_log("Running scrublet on the following batches separately: {}".format(batches))
            for batch in batches:
                X = rna.X.A
                batch_indices = rna.obs["batch"]
                doublet_scores = np.zeros(shape=(X.shape[0]))
                doublet_predictions = np.zeros(shape=(X.shape[0]))
                # run scrublet separately on every batch; should take a couple of seconds
                for b in np.unique(batch_indices.values):
                    mask = batch_indices.values == b
                    scores, predictions = scrublet.Scrublet(X[mask], sim_doublet_ratio=10.).scrub_doublets()
                    doublet_scores[mask] = scores
                    doublet_predictions[mask] = predictions
        else:
            logger.add_to_log("Running scrublet...")
            X = rna.X.A
            doublet_scores, doublet_predictions = scrublet.Scrublet(X, sim_doublet_ratio=10.).scrub_doublets()
            
        rna.obs['doublet_probability'] = doublet_scores
        rna.obs['doublet_prediction'] = doublet_predictions
                
        logger.add_to_log("Removing doublets...")
        n_obs_before = rna.n_obs
        percent_removed = 100*(np.sum(rna.obs['doublet_prediction']!='singlet'))/n_obs_before
        level = "warning" if percent_removed > 40 else "info"
        logger.add_to_log(QC_STRING_DOUBLETS.format(np.sum(rna.obs['doublet_prediction']!='singlet'), percent_removed, rna.n_obs), level=level)
        summary.append("Removed {} estimated doublets.".format(n_obs_before-rna.n_obs))
        if rna.n_obs == 0:
            logger.add_to_log("No cells left after doublet detection. Skipping the next processing steps.", "error")
            summary.append("No cells left after doublet detection.")
        else:
            logger.add_to_log("Normalizing RNA...")
            sc.pp.normalize_total(rna, target_sum=configs["normalize_total_target_sum"])
            sc.pp.log1p(rna)
            rna.raw = rna
            sc.pp.scale(rna, zero_center=False)
            logger.add_to_log("Calculating PCA...")
            sc.pp.pca(rna)
            logger.add_to_log("Calculating neighborhood graph and UMAP based on PCA...")
            key = "pca_neighbors"
            sc.pp.neighbors(rna, n_neighbors=configs["neighborhood_graph_n_neighbors"],
                use_rep="X_pca", key_added=key)
            rna.obsm["X_umap_pca"] = sc.tl.umap(rna, min_dist=configs["umap_min_dist"], spread=float(configs["umap_spread"]),
                                                
                n_components=configs["umap_n_components"], neighbors_key=key, copy=True).obsm["X_umap"]
            logger.add_to_log("Calculating neighborhood graph and UMAP based on SCVI components...")
            key = "scvi_neighbors"
            sc.pp.neighbors(rna, n_neighbors=configs["neighborhood_graph_n_neighbors"],
                use_rep="X_scVI", key_added=key)
            sc.tl.leiden(rna, key_added='overclustering_percolate', resolution=2.0, neighbors_key=key)
            rna.obsm["X_umap_scvi"] = sc.tl.umap(rna, min_dist=configs["umap_min_dist"], spread=float(configs["umap_spread"]),
                n_components=configs["umap_n_components"], neighbors_key=key, copy=True).obsm["X_umap"]
            logger.add_to_log("Calculating neighborhood graph and UMAP based on TOTALVI components...")
            if is_cite:
                key = "totalvi_neighbors"
                sc.pp.neighbors(rna, n_neighbors=configs["neighborhood_graph_n_neighbors"],
                    use_rep="X_totalVI", key_added=key) 
                rna.obsm["X_umap_totalvi"] = sc.tl.umap(rna, min_dist=configs["umap_min_dist"], spread=float(configs["umap_spread"]),
                    n_components=configs["umap_n_components"], neighbors_key=key, copy=True).obsm["X_umap"]
        logger.add_to_log("Gathering data...")
        # copy all filters into adata
        keep = adata.obs.index.isin(rna.obs.index)
        adata = adata[keep,].copy()
        
        adata.obsm.update(rna.obsm)
        adata.obs[rna.obs.columns] = rna.obs
        # save raw rna counts
        adata.obs['sum_percolation_score'] = 0
        if 'percolation_score' in configs:
            for obs_key in configs['percolation_score']:
                if obs_key in adata.obs.columns:
                    percolate_observation(adata, overclustering_key='overclustering_percolate', **configs['percolation_score'][obs_key])
                    adata.obs['sum_percolation_score'] += adata.obs[f'{obs_key}_percolation'].astype(int)
                else:
                    logger.add_to_log(f"Percolation score {obs_key} was not found in obs. Skipping computation.")
        adata.obs['sum_percolation_score'] = adata.obs['sum_percolation_score'].astype('category')
        adata.layers["raw_counts"] = adata.X.copy()
        if adata.n_obs > 0:
            logger.add_to_log("Normalize rna counts in adata.X...")
            sc.pp.normalize_total(adata, target_sum=configs["normalize_total_target_sum"])
            sc.pp.log1p(adata)
    except Exception as err:
        logger.add_to_log("Execution failed with the following error: {}.\n{}".format(err, traceback.format_exc()), "critical")
        logger.add_to_log("Terminating execution prematurely.")
        if not sandbox_mode:
            # upload log to S3
            sync_cmd = 'aws s3 sync --no-progress {} s3://immuneaging/processed_samples/{}/{}/ --exclude "*" --include {}'.format(
                data_dir, prefix, version, logger_file)
            os.system(sync_cmd)
        print(err)
        sys.exit()

logger.add_to_log("Saving h5ad file...")
adata.obs['sample_pipeline_version'] = f"{configs['donor']}__{configs['pipeline_version']}"
adata.obs['sample_code_version'] =  f"{configs['donor']}__{configs['code_version']}"
write_anndata_with_object_cols(adata, data_dir, h5ad_file)

###############################################################
###### OUTPUT UPLOAD TO S3 - ONLY IF NOT IN SANDBOX MODE ######
###############################################################

if not sandbox_mode:
    logger.add_to_log("Uploading h5ad file to S3...")
    sync_cmd = 'aws s3 sync --no-progress {} s3://immuneaging/processed_samples/{}/{}/ --exclude "*" --include {}'.format(data_dir, prefix, version, h5ad_file)
    logger.add_to_log("sync_cmd: {}".format(sync_cmd))
    logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))
    if not no_cells:
        logger.add_to_log("Uploading model files (a single .zip file for each model) to S3...")
        sync_cmd = 'aws s3 sync --no-progress {} s3://immuneaging/processed_samples/{}/{}/ --exclude "*" --include {}'.format(data_dir, prefix, version, scvi_model_file)
        if is_cite:
            # also upload the totalvi file if we had CITE data
            sync_cmd += ' --include {}'.format(totalvi_model_file)
        logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))        
        logger.add_to_log("Uploading decontx model file to S3...")
        sync_cmd = 'aws s3 sync --no-progress {} s3://immuneaging/processed_samples/{}/{}/ --exclude "*" --include {}'.format(
            decontx_data_dir, prefix, version, decontx_model_file.split("/")[-1])
        logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))

logger.add_to_log("Execution of process_sample.py is complete.")

summary.append(QC_STRING_COUNTS.format(adata.n_obs, adata.n_vars))
for i in summary:
    logger.add_to_log(i)

logging.shutdown()
if not sandbox_mode:
    # Uploading log file to S3...
    sync_cmd = 'aws s3 sync --no-progress {} s3://immuneaging/processed_samples/{}/{}/ --exclude "*" --include {}'.format(
        data_dir, prefix, version, logger_file)
    os.system(sync_cmd)
