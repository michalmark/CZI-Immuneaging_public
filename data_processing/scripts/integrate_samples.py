## For speed, this script should be executed on s130 which has a GPU and high RAM
import sys
integrate_samples_script = sys.argv[0]
configs_file = sys.argv[1]

#########################################################
###### INITIALIZATIONS AND PREPARATIONS BEGIN HERE ######
#########################################################

import logging
import os
import json
import scanpy as sc
import numpy as np
import pandas as pd
import scvi
import hashlib
import traceback
import celltypist
import urllib.request
import zipfile
import gc

logging.getLogger('numba').setLevel(logging.WARNING)

# This does two things:.
# 1. Makes the logger look good in a log file
# 2. Changes a bit how torch pins memory when copying to GPU, which allows you to more easily run models in parallel with an estimated 1-5% time hit
scvi.settings.reset_logging_handler()
scvi.settings.dl_pin_memory_gpu_training = False

with open(configs_file) as f: 
    data = f.read()	

configs = json.loads(data)
sandbox_mode = configs["sandbox_mode"] == "True"
apply_filtering = configs["filtering"]["apply_filtering"] == "True"

sys.path.append(configs["code_path"])
from utils import *
from vdj_utils import *
from logger import SimpleLogger

output_destination = configs["output_destination"]
s3_access_file = configs["s3_access_file"]

# sort the sample ids lexicographically (and accordingly the versions) in order to avoid generating a new version if the exact same set of samples were previously used but in a different order
order = np.argsort(configs["sample_ids"].split(","))
all_sample_ids = np.array(configs["sample_ids"].split(","))[order]
processed_sample_configs_version = np.array(configs["processed_sample_configs_version"].split(","))[order]
# the followings are required because we check if the integration of the requested samples already exist on aws
configs["sample_ids"] = ",".join(all_sample_ids)
configs["processed_sample_configs_version"] = ",".join(processed_sample_configs_version)

VARIABLE_CONFIG_KEYS = ["data_owner","s3_access_file","code_path","output_destination"] # config changes only to these fields will not initialize a new configs version
sc.settings.verbosity = 3   # verbosity: errors (0), warnings (1), info (2), hints (3)

# apply the aws credentials to allow access though aws cli; make sure the user is authorized to run in non-sandbox mode if applicable
s3_dict = set_access_keys(s3_access_file, return_dict = True)
assert sandbox_mode or hashlib.md5(bytes(s3_dict["AWS_SECRET_ACCESS_KEY"], 'utf-8')).hexdigest() in AUTHORIZED_EXECUTERS, "You are not authorized to run this script in a non sandbox mode; please set sandbox_mode to True"
set_access_keys(s3_access_file)

# create a new directory for the data and outputs
data_dir = os.path.join(output_destination, configs["output_prefix"])
os.system("mkdir -p " + data_dir)

# check for previous versions of integrated data
s3_url = "s3://immuneaging/integrated_samples/{}_level".format(configs["integration_level"])
is_new_version, version = get_configs_status(configs, s3_url + "/" + configs["output_prefix"], "integrate_samples.configs."+configs["output_prefix"],
    VARIABLE_CONFIG_KEYS, data_dir)
output_configs_file = "integrate_samples.configs.{}.{}.txt".format(configs["output_prefix"],version)

# set up logger
logger_file = "integrate_samples.{}.{}.log".format(configs["output_prefix"],version)
logger_file_path = os.path.join(data_dir, logger_file)
if os.path.isfile(logger_file_path):
    os.remove(logger_file_path)

output_h5ad_file = "{}.{}.h5ad".format(configs["output_prefix"], version)
output_h5ad_model_file = "{}.{}.model_data.h5ad".format(configs["output_prefix"], version)

output_h5ad_file_unstim = "{}.unstim.{}.h5ad".format(configs["output_prefix"], version)
output_h5ad_model_file_unstim = "{}.unstim.{}.model_data.h5ad".format(configs["output_prefix"], version)

if configs['include_stim']:
    output_h5ad_file_stim = "{}.stim.{}.h5ad".format(configs["output_prefix"], version)
    output_h5ad_model_file_stim = "{}.stim.{}.model_data.h5ad".format(configs["output_prefix"], version)

logger = SimpleLogger(filename = logger_file_path)
logger.add_to_log("Running integrate_samples.py...")
logger.add_to_log("Starting time: {}".format(get_current_time()))
with open(integrate_samples_script, "r") as f:
    logger.add_to_log("integrate_samples.py md5 checksum: {}\n".format(hashlib.md5(bytes(f.read(), 'utf-8')).hexdigest()))

logger.add_to_log("using the following configurations:\n{}".format(str(configs)))
logger.add_to_log("Configs version: " + version)
logger.add_to_log("New configs version: " + str(is_new_version))

if is_new_version:
    if not sandbox_mode:
        logger.add_to_log("Uploading new configs version to S3...")
        with open(os.path.join(data_dir,output_configs_file), 'w') as f:
            json.dump(configs, f)
        sync_cmd = 'aws s3 sync --no-progress {} {}/{}/{} --exclude "*" --include {}'.format(
            data_dir, s3_url, configs["output_prefix"], version, output_configs_file)
        logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))
else:
    logger.add_to_log("Checking if h5ad file already exists on S3...")
    h5ad_file_exists = False
    logger_file_exists = False
    ls_cmd = "aws s3 ls {}/{}/{} --recursive".format(s3_url, configs["output_prefix"],version)
    files = os.popen(ls_cmd).read()
    logger.add_to_log("aws response: {}\n".format(files))
    for f in files.rstrip().split('\n'):
        if f.split('/')[-1] == output_h5ad_file:
            h5ad_file_exists = True
        if f.split('/')[-1] == logger_file:
            logger_file_exists = True
    if h5ad_file_exists and logger_file_exists:
        logger.add_to_log("The following h5ad file is already on S3: {}\nTerminating execution.".format(output_h5ad_file))
        sys.exit()
    if h5ad_file_exists and not logger_file_exists:
        logger.add_to_log("The following h5ad file is already on S3: {}\nhowever, log file is missing on S3; proceeding with execution.".format(output_h5ad_file))
    if not h5ad_file_exists:
        logger.add_to_log("The following h5ad file does not exist on S3: {}".format(output_h5ad_file))

samples = read_immune_aging_sheet("Samples")

logger.add_to_log("Downloading h5ad files of processed samples from S3...")
all_h5ad_files = []
# for collecting the sample IDs of unstim and stim samples for which we will generate an additional, separate integration:
unstim_sample_ids = [] 
unstim_h5ad_files = []
if configs['include_stim']:
    stim_sample_ids = [] 
    stim_h5ad_files = []
if configs['folder_local_files'].split('.')[-1]=='h5ad':
    preexisting_h5ad = True
    adata = sc.read(configs['folder_local_files'])
else:
    preexisting_h5ad = False
    local_files = os.listdir(configs['folder_local_files']) if configs['folder_local_files'] else []
    for j in range(len(all_sample_ids)):
        sample_id = all_sample_ids[j]
        sample_version = processed_sample_configs_version[j]
        sample_h5ad_file = "{}_GEX.processed.{}.h5ad".format(sample_id,sample_version)
        sample_h5ad_path = os.path.join(data_dir,sample_h5ad_file)
        all_h5ad_files.append(sample_h5ad_path)
        stim_status = samples["Stimulation"][samples["Sample_ID"] == sample_id].values[0]
        if stim_status == "Nonstim":
            unstim_sample_ids.append(sample_id)
            unstim_h5ad_files.append(sample_h5ad_path)
        elif configs['include_stim']:
            stim_sample_ids.append(sample_id)
            stim_h5ad_files.append(sample_h5ad_path)
        if sample_h5ad_file in local_files:
            if os.path.samefile(configs['folder_local_files'], data_dir):
                sync_cmd = f'echo "{sample_h5ad_file} exists in {data_dir}"'
            else:
                sync_cmd = f'cp {configs["folder_local_files"]}/{sample_h5ad_file} {data_dir}; echo Copied {sample_h5ad_file} from {configs["folder_local_files"]}'
        else:
            sync_cmd = 'aws s3 cp --no-progress s3://immuneaging/processed_samples/{}_GEX/{}/ {} --exclude "*" --include {}'.format(
            sample_id,sample_version,data_dir,sample_h5ad_file)
            logger.add_to_log("syncing {}...".format(sample_h5ad_file))
            logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        aws_response = os.popen(sync_cmd).read()
        logger.add_to_log(f"aws response: {aws_response}")
        if not os.path.exists(sample_h5ad_path):
            logger.add_to_log("h5ad file does not exist on aws for sample {}. Terminating execution.".format(sample_id))
            sys.exit()

if configs["integration_level"] == "compartment":
    compartment = configs["output_prefix"]
    logger.add_to_log(f"Downloading csv file containing cell barcodes for the {compartment} compartment from S3...")
    barcodes_csv_file = configs['compartment_barcode_csv_file']
    aws_sync("s3://immuneaging/per-compartment-barcodes/", data_dir, barcodes_csv_file, logger)
    compartment_barcodes_all = pd.read_csv(f"{data_dir}/{barcodes_csv_file}", index_col=0, header=0)
    compartment_barcodes_all.iloc[:, 0] = compartment_barcodes_all.iloc[:, 0].str.upper()
    
    compartment_barcodes = list(compartment_barcodes_all[compartment_barcodes_all.iloc[:, 0]==compartment[0]].index)
    
    logger.add_to_log(f"Downloading csv file containing cell barcodes to exclude, if any...")
    # exclude_barcodes_csv_file = f"Exclude-barcodes.csv"
    # aws_sync("s3://immuneaging/per-compartment-barcodes/", data_dir, exclude_barcodes_csv_file, logger)
    # exclude_barcodes_csv_path = os.path.join(data_dir, exclude_barcodes_csv_file)
    
filter_dir = f"{data_dir}/filter_{configs['filtering']['filter_name']}"
os.system("mkdir -p " + filter_dir)
if configs["integration_level"] == "tissue":
    tissue = samples["Organ"][samples["Sample_ID"] == sample_id].values[0]
    filter_files = f"{tissue}*.csv"
else:
    filter_files = "*.csv"
aws_sync(f"s3://immuneaging/per-compartment-barcodes/filter_{configs['filtering']['filter_name']}",
            f"{data_dir}/filter_{configs['filtering']['filter_name']}", filter_files, logger)

filter_barcodes = []
for file in os.listdir(filter_dir):
    if 'csv' in file:
        filter_barcodes += list(pd.read_csv(f"{filter_dir}/{file}", index_col=0).index)
logger.add_to_log(f"Total number of cells in low quality filter is {len(filter_barcodes)}")

############################################
###### SAMPLE INTEGRATION BEGINS HERE ######
############################################

# run the following integration pipeline three times if there is a combination of stim and unstim samples - once using the stim and unstim samples, once using unstim only, once using stim only
if configs['include_stim']:
    integration_modes = ["stim_unstim"]
    if (len(unstim_sample_ids) > 0) and (len(all_sample_ids)-len(unstim_sample_ids)>0):
        integration_modes += ["unstim", "stim"]
else:
    integration_modes = ["unstim"]

valid_libs = get_all_libs("GEX")
for integration_mode in integration_modes:
    logger.add_to_log("Running {} integration pipeline...".format(integration_mode))
    if integration_mode == "stim_unstim":
        sample_ids = all_sample_ids
        h5ad_files = all_h5ad_files
        mode_suffix = ""
    elif integration_mode == "unstim":
        sample_ids = unstim_sample_ids
        h5ad_files = unstim_h5ad_files
        mode_suffix = ".unstim"
    else:
        sample_ids = stim_sample_ids
        h5ad_files = stim_h5ad_files
        mode_suffix = ".stim"
    prefix = configs["output_prefix"] + mode_suffix
    dotplot_dirname = "dotplots" + mode_suffix
    output_h5ad_file = "{}.{}.h5ad".format(prefix, version)
    output_h5ad_file_cleanup = "{}_cleanup.{}.h5ad".format(prefix, version)
    logger.add_to_log("Reading h5ad files of processed samples...")
    
    if preexisting_h5ad == False:
        adata_dict = {}
        for j in range(len(h5ad_files)):
            h5ad_file = h5ad_files[j]
            sample_id = sample_ids[j]
            if configs["integration_level"] == "compartment":
                adata_temp = sc.read_h5ad(h5ad_file)
                idx = adata_temp.obs_names.isin(compartment_barcodes)
                print(idx)
                print(adata_temp[idx, :])

                if np.sum(idx) > 2: # i.e. if there are at least three cells that passes the condition above
                    adata_dict[sample_id] = adata_temp[idx, :].copy()
                    del adata_temp
                    gc.collect()
                else:
                    del adata_temp
                    gc.collect()
                    continue
            else:
                adata_dict[sample_id] = sc.read_h5ad(h5ad_file)
            # add the tissue to the adata since we may need it as part of a composite batch_key later
            adata_dict[sample_id].obs["tissue"] = samples["Organ"][samples["Sample_ID"] == sample_id].values[0]
            adata_dict[sample_id].obs["sample_id"] = sample_id
            for field in ('X_pca', 'X_scVI', 'X_totalVI', 'X_umap_pca', 'X_umap_scvi', 'X_umap_totalvi'):
                if field in adata_dict[sample_id].obsm:
                    del adata_dict[sample_id].obsm[field]
            adata_dict[sample_id].X = adata_dict[sample_id].layers['raw_counts']
            if configs["integration_level"] != "tissue":
                del adata_dict[sample_id].layers
            del adata_dict[sample_id].obsp
        if configs["integration_level"] == "tissue" and len(adata_dict) == 0:
            msg = f"No cells found for tissue {compartment} from all samples in {integration_mode} integration mode..."
            if integration_mode != "stim_unstim":
                logger.add_to_log(msg, level="warning")
                # move on to the next integration mode
                continue
            else:
                logger.add_to_log(msg + " Terminating execution.", level="error")
                logging.shutdown()
                if not sandbox_mode:
                    # upload log to S3
                    aws_sync(data_dir, "{}/{}/{}/".format(s3_url, configs["output_prefix"], version), logger_file, logger, do_log=False)
                sys.exit()
        sample_ids = list(adata_dict.keys())
        # get the names of all proteins and control proteins across all samples (in case the samples were collected using more than one protein panel)
        proteins = set()
        proteins_ctrl = set()
        for sample_id in sample_ids:
            if "protein_expression" in adata_dict[sample_id].obsm:
                proteins.update(adata_dict[sample_id].obsm["protein_expression"].columns)
            if "protein_expression_Ctrl" in adata_dict[sample_id].obsm:
                proteins_ctrl.update(adata_dict[sample_id].obsm["protein_expression_Ctrl"].columns)
        # each sample should include all proteins; missing values are set as NaN
        if len(proteins) > 0 or len(proteins_ctrl) > 0:
            proteins = list(proteins)
            proteins_ctrl = list(proteins_ctrl)
            for sample_id in sample_ids:
                df = pd.DataFrame(columns = proteins, index = adata_dict[sample_id].obs.index)
                if "protein_expression" in adata_dict[sample_id].obsm:
                    df[adata_dict[sample_id].obsm["protein_expression"].columns] = adata_dict[sample_id].obsm["protein_expression"].copy()
                adata_dict[sample_id].obsm["protein_expression"] = df
                df_ctrl = pd.DataFrame(columns = proteins_ctrl, index = adata_dict[sample_id].obs.index)
                if "protein_expression_Ctrl" in adata_dict[sample_id].obsm:
                    df_ctrl[adata_dict[sample_id].obsm["protein_expression_Ctrl"].columns] = adata_dict[sample_id].obsm["protein_expression_Ctrl"].copy()
                adata_dict[sample_id].obsm["protein_expression_Ctrl"] = df_ctrl
        logger.add_to_log("Concatenating all datasets...")
        adata = adata_dict[sample_ids[0]]
        if len(sample_ids) > 1:
            adata = adata.concatenate([adata_dict[sample_ids[j]] for j in range(1,len(sample_ids))], join="outer", index_unique=None)
        del adata_dict
        gc.collect()
        # Move the summary statistics of the genes (under .var) to a separate csv file
        cols_to_varm = [j for j in adata.var.columns if "n_cells" in j] + \
        [j for j in adata.var.columns if "mean_counts" in j] + \
        [j for j in adata.var.columns if "pct_dropout_by_counts" in j] + \
        [j for j in adata.var.columns if "total_counts" in j]
        output_gene_stats_csv_file = "{}.{}.gene_stats.csv".format(prefix, version)
        adata.var.iloc[:,adata.var.columns.isin(cols_to_varm)].to_csv(os.path.join(data_dir,output_gene_stats_csv_file))
        adata.var = adata.var.drop(labels = cols_to_varm, axis = "columns")
        if not sandbox_mode:
            sync_cmd = 'aws s3 sync --no-progress {} {}/{}/{} --exclude "*" --include {}'.format(
                data_dir, s3_url, configs["output_prefix"], version, output_gene_stats_csv_file)
            logger.add_to_log("sync_cmd: {}".format(sync_cmd))
            logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))
        
        write_anndata_with_object_cols(adata, data_dir, "concatenated_data_before_processing.h5ad")
    else:
        if configs["integration_level"] == "compartment":
            adata = adata[adata.obs_names.isin(compartment_barcodes)]
    
    if apply_filtering:
        n_obs = len(adata.obs_names)
        logger.add_to_log(f"{len(set(filter_barcodes) - set(adata.obs_names))}: Cells in filter were not part of the adata object.")
        adata = adata[list(set(adata.obs_names) - set(filter_barcodes))]
        logger.add_to_log(f"Applied low quality filter and removed {n_obs - len(adata.obs_names)} cells.")
    else:
        adata.obs['loaded_filter'] = "filtered"
        adata.obs.loc[list(set(adata.obs_names) - set(filter_barcodes)), 'loaded_filter'] = "pass filtering"   
    # protein QC
    protein_levels_max_sds = configs["protein_levels_max_sds"] if "protein_levels_max_sds" in configs else None
    n_cells_before, n_proteins_before = adata.obsm["protein_expression"].shape
    if n_proteins_before > 0 and protein_levels_max_sds is not None:
        logger.add_to_log("Running protein QC...")
        # (1) remove cells that demonstrate extremely high (or low) protein library size; normalize library size by the total number of non NaN proteins available for the cell.
        prot_exp = adata.obsm["protein_expression"]
        normalized_cell_lib_size = prot_exp.sum(axis=1)/(prot_exp.shape[1] - prot_exp.isnull().sum(axis=1))
        keep, lower_bound, upper_bound = detect_outliers(normalized_cell_lib_size, protein_levels_max_sds)
        # save the qc thresholds in .uns so they can later be applied to other data if needed (specifically, in case of integrating new data into the model we learn here)
        adata.uns["protein_qc"] = {}
        adata.uns["protein_qc"]["normalized_cell_lib_size_lower_bound"] = lower_bound
        adata.uns["protein_qc"]["normalized_cell_lib_size_upper_bound"] = upper_bound
        logger.add_to_log("Removing {} cells with extreme protein values (normalized library size more extreme than {} standard deviations)...".format(np.sum(~keep), protein_levels_max_sds))
        adata = adata[keep,].copy()
        # (2) remove proteins that have extreme library size (across all cells; consider only cells that have non-missing values and normalize by the number of cells with no missing values for the protein)
        prot_exp = adata.obsm["protein_expression"]
        normalized_protein_lib_sizes = prot_exp.sum()/(prot_exp.shape[0] - prot_exp.isnull().sum(axis=0))
        keep, lower_bound, upper_bound = detect_outliers(normalized_protein_lib_sizes, protein_levels_max_sds)
        adata.uns["protein_qc"]["normalized_protein_lib_size_lower_bound"] = lower_bound
        adata.uns["protein_qc"]["normalized_protein_lib_size_upper_bound"] = upper_bound
        logger.add_to_log("Removing {} proteins with extreme total number of reads across cells (normalized levels - by the number of cells with no missing values for the protein - more extreme than {} standard deviations)...".format(
            np.sum(~keep), protein_levels_max_sds))
        cols_keep = adata.obsm["protein_expression"].columns[keep.values]
        cols_remove = adata.obsm["protein_expression"].columns[~keep.values]
        logger.add_to_log("Removing proteins: {} ".format(", ".join(cols_remove.values)))
        extend_removed_features_df(adata, "removed_proteins", adata.obsm["protein_expression"][cols_remove])
        adata.obsm["protein_expression"] = adata.obsm["protein_expression"][cols_keep]
    # remove proteins that were left with zero reads
    non_zero_proteins = adata.obsm["protein_expression"].sum() > 0
    num_zero_proteins = np.sum(~non_zero_proteins)
    cols_keep = adata.obsm["protein_expression"].columns[non_zero_proteins.values]
    cols_remove = adata.obsm["protein_expression"].columns[~non_zero_proteins.values]
    if num_zero_proteins > 0:
        logger.add_to_log("Removing {} proteins with zero reads across all cells...".format(num_zero_proteins))
        logger.add_to_log("Removing proteins: {} ".format(", ".join(cols_remove.values)))
    extend_removed_features_df(adata, "removed_proteins", adata.obsm["protein_expression"][cols_remove])
    adata.obsm["protein_expression"] = adata.obsm["protein_expression"][cols_keep]
    # summarize protein qc
    logger.add_to_log("Protein QC summary: a total of {} cells ({}%) and {} proteins ({}%) were filtered out owing to extreme values.".format(
        n_cells_before-adata.n_obs, round(100*(n_cells_before-adata.n_obs)/n_cells_before,2),
        n_proteins_before-adata.obsm["protein_expression"].shape[1], round(100*(n_proteins_before-adata.obsm["protein_expression"].shape[1])/n_proteins_before,2)))
    # end protein QC
    if "is_solo_singlet" in adata.obs:
        del adata.obs["is_solo_singlet"]
    if "Classification" in adata.obs:
        del adata.obs["Classification"]
    logger.add_to_log("A total of {} cells and {} genes are available after merge.".format(adata.n_obs, adata.n_vars))
    # set the train size; this train size was justified by an experiment that is described here https://yoseflab.github.io/scvi-tools-reproducibility/runtime_analysis_reproducibility/
    configs["train_size"] = 0.9 if 0.1 * adata.n_obs < 20000 else 1-(20000/adata.n_obs)
    try:
        is_cite = "protein_expression" in adata.obsm
        if is_cite:
            logger.add_to_log("Detected Antibody Capture features.")
        # iterate over batch keys
        batch_keys = ["batch"] if "batch_key" not in configs else configs["batch_key"].split(",")
        scvi_model_files = {}
        totalvi_model_files = {}
        run_pca = True
        for batch_key in batch_keys:
            if batch_key == 'seq_batch':
                dir_path = os.path.dirname(os.path.realpath(__file__))
                with open(os.path.join(dir_path, "rename_dictionaries.json"), 'r') as j:
                    extra_setting = json.loads(j.read())
                adata.uns['seq_batch_dict'] = extra_setting['seq_batch']
                adata.obs['seq_batch'] = adata.obs['sample_id']
                adata.obs['seq_batch'] = adata.obs['seq_batch'].replace(adata.uns['seq_batch_dict'])
                adata.obs['seq_batch'] = adata.obs['seq_batch'].astype('category')
            logger.add_to_log("Running for batch_key {}...".format(batch_key))
            rna = adata.copy()
            if batch_key not in rna.obs:
                if batch_key == "donor_id+tissue":
                    rna.obs[batch_key] = rna.obs["donor_id"].astype("str") + "+" + rna.obs["tissue"].astype("str")
                    # we need to remove cells, if any, that belong to a batch that has a size one
                    # or else the scanpy hvg call below fails. It is ok if there is only ever one or
                    # two such cells, but not if there is a lot of them, which is why we emit a warning
                    # log.
                    batch_vc = rna.obs["donor_id+tissue"].value_counts()
                    for b in batch_vc[batch_vc == 1].index.values:
                        barcode = rna[rna.obs[batch_key] == b].obs.index[0]
                        logger.add_to_log(f"Removing cell {barcode} where {batch_key} = {b} b/c it's the only one of its batch.", level="warning")
                        keep_idx = (rna.obs[batch_key] != b)
                        rna = rna[keep_idx,:].copy()
                        # need to also remove it from adata
                        adata = adata[keep_idx,:].copy()
                else:
                    logger.add_to_log(f"Batch key {batch_key} not found in adata columns. Terminating execution.", level="error")
                    logging.shutdown()
                    if not sandbox_mode:
                        # upload log to S3
                        aws_sync(data_dir, "{}/{}/{}/".format(s3_url, configs["output_prefix"], version), logger_file, logger, do_log=False)
                    sys.exit()
            logger.add_to_log("Filtering out vdj genes...")
            rna = filter_vdj_genes(rna, configs["vdj_genes"], data_dir, logger)
            rna.layers["log1p_transformed"] = rna.X.copy()
            sc.pp.normalize_total(rna, layers=["log1p_transformed"])
            sc.pp.log1p(rna, layer="log1p_transformed")
            logger.add_to_log("Detecting highly variable genes...")
            if configs["highly_variable_genes_flavor"] == "seurat_v3":
                # highly_variable_genes requires counts data in this case
                layer = None
            else:
                layer = "log1p_transformed"
            sc.pp.highly_variable_genes(
                rna,
                n_top_genes=configs["n_highly_variable_genes"],
                subset=False,
                flavor=configs["highly_variable_genes_flavor"],
                batch_key=batch_key,
                span=1.0,
                layer=layer)

            sc.pp.highly_variable_genes(
                rna,
                n_top_genes=configs["n_highly_variable_genes"],
                subset=False,
                flavor=configs["highly_variable_genes_flavor"],
                span=1.0,
                layer=layer)
            rna = rna[:, np.logical_and(rna.var['highly_variable']==True, rna.var['highly_variable_nbatches']>max(min(0.9*len(rna.obs[batch_key].unique()), 1.5), 0.2*len(rna.obs[batch_key].unique())))].copy()
            # scvi
            key = f"X_scvi_integrated_batch_key_{batch_key}"
            _, scvi_model_file = run_model(rna, configs, batch_key, None, "scvi", prefix, version, data_dir, logger, key)
            scvi_model_files[batch_key] = scvi_model_file
            logger.add_to_log("Calculate neighbors graph and UMAP based on scvi components...")
            neighbors_key = f"scvi_integrated_neighbors_batch_key_{batch_key}"
            sc.pp.neighbors(rna, n_neighbors=configs["neighborhood_graph_n_neighbors"], use_rep=key, key_added=neighbors_key) 
            rna.obsm[f"X_umap_scvi_integrated_batch_key_{batch_key}"] = sc.tl.umap(
                rna,
                min_dist=configs["umap_min_dist"],
                spread=float(configs["umap_spread"]),
                n_components=configs["umap_n_components"],
                neighbors_key=neighbors_key,
                copy=True
            ).obsm["X_umap"]
            if is_cite and configs["integration_level"] == "tissue":
                # totalVI
                key = f"X_totalVI_integrated_batch_key_{batch_key}"
                # if there is no protein information for some of the cells set them to zero (instead of NaN)
                rna.obsm["protein_expression"] = rna.obsm["protein_expression"].fillna(0)
                # there are known spurious failures with totalVI (such as "invalid parameter loc/scale")
                # so we try a few times then carry on with the rest of the script as we can still mine the
                # rest of the data regardless of CITE info
                retry_count = 4
                try:
                    _, totalvi_model_file = run_model(rna, configs, batch_key, "protein_expression", "totalvi", prefix, version, data_dir, logger, latent_key=key, max_retry_count=retry_count)
                    totalvi_model_files[batch_key] = totalvi_model_file
                    logger.add_to_log("Calculate neighbors graph and UMAP based on totalVI components...")
                    neighbors_key = f"totalvi_integrated_neighbors_batch_key_{batch_key}"
                    sc.pp.neighbors(rna, n_neighbors=configs["neighborhood_graph_n_neighbors"],use_rep=key, key_added=neighbors_key) 
                    rna.obsm[f"X_umap_totalvi_integrated_batch_key_{batch_key}"] = sc.tl.umap(
                        rna,
                        min_dist=configs["umap_min_dist"],
                        spread=float(configs["umap_spread"]),
                        n_components=configs["umap_n_components"],
                        neighbors_key=neighbors_key,
                        copy=True
                    ).obsm["X_umap"]
                except Exception as err:
                    logger.add_to_log("Execution of totalVI failed with the following error (latest) with retry count {}: {}. Moving on...".format(retry_count, err), "warning")
            if run_pca:
                logger.add_to_log("Calculating PCA...")
                rna.obsm['X_pca'] = sc.pp.pca(rna.layers['log1p_transformed'])
                logger.add_to_log("Calculating neighborhood graph and UMAP based on PCA...")
                key = "pca_neighbors"
                sc.pp.neighbors(rna, n_neighbors=configs["neighborhood_graph_n_neighbors"], use_rep="X_pca", key_added=key) 
                rna.obsm["X_umap_pca"] = sc.tl.umap(
                    rna,
                    min_dist=configs["umap_min_dist"],
                    spread=float(configs["umap_spread"]),
                    n_components=configs["umap_n_components"],
                    neighbors_key=key,
                    copy=True
                ).obsm["X_umap"]
                run_pca = False
            # update the adata with the components of the dim reductions and umap coordinates
            adata.obsm.update(rna.obsm)
            # save the identity of the most variable genes used
            adata.var[f"is_highly_variable_gene_batch_key_{batch_key}"] = adata.var.index.isin(rna.var.index)
    except Exception as err:
        logger.add_to_log("Execution failed with the following error: {}.\n{}".format(err, traceback.format_exc()), "critical")
        logger.add_to_log("Terminating execution prematurely.")
        if not sandbox_mode:
            # upload log to S3
            sync_cmd = 'aws s3 sync --no-progress {} {}/{}/{}/ --exclude "*" --include {}'.format( \
                data_dir, s3_url, configs["output_prefix"], version, logger_file)
            os.system(sync_cmd)
        print(err)
        sys.exit()
    logger.add_to_log("Using CellTypist for annotations...")
    # remove celltypist predictions and related metadata that were added at the sample-level processing
    celltypist_cols = [j for j in adata.obs.columns if "celltypist" in j]
    adata.obs = adata.obs.drop(labels = celltypist_cols, axis = "columns")
    leiden_resolutions = [float(j) for j in configs["leiden_resolutions"].split(",")]
    celltypist_model_urls = configs["celltypist_model_urls"].split(",")
    celltypist_dotplot_min_frac = configs["celltypist_dotplot_min_frac"]
    logger.add_to_log("Downloading CellTypist models...")
    # download celltypist models
    celltypist_model_paths = []
    for celltypist_model_url in celltypist_model_urls:
        celltypist_model_path = os.path.join(data_dir, celltypist_model_url.split("/")[-1])
        urllib.request.urlretrieve(celltypist_model_url, celltypist_model_path)
        celltypist_model_paths.append(celltypist_model_path)
    dotplot_paths = []
    for batch_key in batch_keys:
        dotplot_paths += annotate(
            adata,
            model_paths = celltypist_model_paths,
            model_urls = celltypist_model_urls,
            components_key = f"X_scvi_integrated_batch_key_{batch_key}",
            neighbors_key = f"neighbors_scvi",
            n_neighbors = configs["neighborhood_graph_n_neighbors"],
            resolutions = leiden_resolutions,
            model_name = f"scvi_batch_key_{batch_key}" + mode_suffix,
            dotplot_min_frac = celltypist_dotplot_min_frac,
            logger = logger,
            save_all_outputs = True
        )
        totalvi_key = f"X_totalVI_integrated_batch_key_{batch_key}"
        if totalvi_key in adata.obsm:
            dotplot_paths += annotate(
                adata,
                model_paths = celltypist_model_paths,
                model_urls = celltypist_model_urls,
                components_key = totalvi_key,
                neighbors_key = "neighbors_totalvi",
                n_neighbors = configs["neighborhood_graph_n_neighbors"],
                resolutions = leiden_resolutions,
                model_name = f"totalvi_batch_key_{batch_key}" + mode_suffix,
                dotplot_min_frac = celltypist_dotplot_min_frac,
                logger = logger,
            )
    if configs["integration_level"] == "tissue":
        sc.pp.neighbors(adata, n_neighbors=configs["neighborhood_graph_n_neighbors"],
                    use_rep=f"X_scvi_integrated_batch_key_{batch_key}", key_added="overclustering") 
        sc.tl.leiden(adata, key_added='overclustering_tissue_percolate', resolution=5.0, neighbors_key="overclustering")
        adata.obs['sum_percolation_score'] = adata.obs['sum_percolation_score'].astype(float)
        
        df = adata.obs.groupby('overclustering_tissue_percolate')['sum_percolation_score'].agg(['median', 'mean', lambda x: x.quantile(0.75)])
        for cluster in df.index:
            adata.obs.loc[adata.obs['overclustering_tissue_percolate'] == cluster, 'sum_percolation_score_q75_cluster'] = df.loc[cluster, '<lambda_0>']
            adata.obs.loc[adata.obs['overclustering_tissue_percolate'] == cluster, 'sum_percolation_score_median_cluster'] = df.loc[cluster, 'median']
            adata.obs.loc[adata.obs['overclustering_tissue_percolate'] == cluster, 'sum_percolation_score_mean_cluster'] = df.loc[cluster, 'mean']
        if "filtering" in configs and not apply_filtering:
            tissue = adata.obs['tissue'].iloc[0]
            adata.obs["to_filter"] = "pass filtering"  
            
            for key, value in configs["filtering"]["percolation_score_median"].items():
                adata.obs.loc[adata.obs[key] < value, "to_filter"] = "filtered"
            threshold = configs["filtering"]["sum_percolation_score_mean_cluster"][tissue]
            adata.obs.loc[adata.obs["sum_percolation_score_mean_cluster"] > threshold, "to_filter"] = "filtered"
            
            celltypist_key = 'celltypist_majority_voting.Immune_All_High.scvi_batch_key_donor_id.unstim.leiden_resolution_10.0'
            key = tissue if tissue in configs["filtering"]["celltypes_passing_filtering"] else "all"
            
            adata.obs.loc[adata.obs[celltypist_key].isin(configs["filtering"]["celltypes_passing_filtering"][key]), "to_filter"] = "pass filtering"
            adata.obs.loc[adata.obs["to_filter"]=='filtered', "to_filter"].to_csv(os.path.join(data_dir,f'{tissue}_low_quality_filter.csv'))
    
    dotplot_dir = os.path.join(data_dir,dotplot_dirname)
    os.system("rm -r -f {}".format(dotplot_dir))
    os.system("mkdir {}".format(dotplot_dir))
    for dotplot_path in dotplot_paths:
        os.system("mv {} {}".format(dotplot_path, dotplot_dir))
    dotplots_zipfile = "{}.{}.celltypist_dotplots.zip".format(prefix, version)
    zipf = zipfile.ZipFile(os.path.join(data_dir,dotplots_zipfile), 'w', zipfile.ZIP_DEFLATED)
    zipdir(dotplot_dir, zipf)
    zipf.close()
    logger.add_to_log("Adding bcr_lib_id and tcr_lib_id to adata where applicable...")
    gex_to_bcr, gex_to_tcr = get_gex_lib_to_vdj_lib_mapping()
    add_vdj_lib_ids_to_adata(adata, gex_to_bcr, gex_to_tcr)
    logger.add_to_log("Saving h5ad files...")
    adata.obs["age"] = adata.obs["age"].astype(str)
    adata.obs["BMI"] = adata.obs["BMI"].astype(str)
    adata.obs["height"] = adata.obs["height"].astype(str)
    write_anndata_with_object_cols(adata, data_dir, output_h5ad_file)
    write_anndata_with_object_cols(adata, data_dir, output_h5ad_file_cleanup, cleanup=True)
    # OUTPUT UPLOAD TO S3 - ONLY IF NOT IN SANDBOX MODE
    if not sandbox_mode:
        logger.add_to_log("Uploading h5ad file to S3...")
        sync_cmd = 'aws s3 sync --no-progress {} {}/{}/{} --exclude "*" --include {} --include {}'.format(
            data_dir, s3_url, configs["output_prefix"], version, output_h5ad_file, output_h5ad_file_cleanup)
        logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))
        logger.add_to_log("Uploading model files (a single .zip file for each model) and CellTypist dot plots to S3...")
        inclusions = [f"--include {file}" for file in list(scvi_model_files.values()) + list(totalvi_model_files.values())]
        sync_cmd = 'aws s3 sync --no-progress {} {}/{}/{}/ --exclude "*" {}'.format(
            data_dir, s3_url, configs["output_prefix"], version, " ".join(inclusions))
        sync_cmd += ' --include {}'.format(dotplots_zipfile)         
        logger.add_to_log("sync_cmd: {}".format(sync_cmd))
        logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))
        logger.add_to_log("Uploading gene stats csv file to S3...")
        if "filtering" in configs and not configs["filtering"]["apply_filtering"]:
            sync_cmd = 'aws s3 sync --no-progress {} {}/{}/ --exclude "*" --include {}'.format(
                data_dir, "s3://immuneaging/per-compartment-barcodes", 
                f"filter_{configs['filtering']['filter_name']}", 
                f'{tissue}_low_quality_filter.csv')
            logger.add_to_log("sync_cmd: {}".format(sync_cmd))
            logger.add_to_log("aws response: {}\n".format(os.popen(sync_cmd).read()))
            
    logger.add_to_log("Number of cells: {}, number of genes: {}.".format(adata.n_obs, adata.n_vars))

logger.add_to_log("Execution of integrate_samples.py is complete.")

logging.shutdown()
if not sandbox_mode:
    # Uploading log file to S3.
    sync_cmd = 'aws s3 sync --no-progress {} {}/{}/{} --exclude "*" --include {}'.format(
        data_dir, s3_url, configs["output_prefix"], version, logger_file)
    os.system(sync_cmd)
