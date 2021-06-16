import json
import os
import time
import logging
import glob

def get_current_time():
	return time.strftime("%H:%M, %m-%d-%Y")

def start_logger(level, filename):
	for handler in logging.root.handlers:
		logging.root.removeHandler(handler)
	logging.basicConfig(level = level, filename = filename)

def add_to_log(s, level = "info"):
	toprint = time.strftime("%H:%M,%m-%d-%Y:") + s
	if level == "info":
		logging.info(toprint)
	elif level == "debug":
		logging.debug(toprint)
		
def set_access_keys(filepath, return_dict = False):
	"""
	Sets the user's access keys to the AWS S3 bucket.

	Assumes this file includes only two uncommented lines with keys:
	export AWS_ACCESS_KEY_ID=<key>
	export AWS_SECRET_ACCESS_KEY=<key>

	If return_dict == True then only returns the dictionary.
	"""
	keys = {}
	with open(filepath) as fp:
		for i in fp:
			if len(i.rstrip()) and i[0]!="#":
				cmd = i.rstrip().split(" ")
				assert(cmd[0] == "export")
				cmd.pop(0)
				for i in range(len(cmd)-1,-1,-1):
					if len(cmd[i]) == 0:
						cmd.pop(i)
				pair = "".join(cmd).split("=")
				keys[pair[0]] = pair[1]
	if return_dict:
		return(keys)
	for k in keys:
		os.environ[k] = keys[k]
	return

def load_configs(filename):
    with open(filename) as f: 
        data = f.read()	
    configs = json.loads(data)
    #validate_configs(configs)
    return configs

#def validate_configs(configs):
#	assert(configs["aligner"] == "cellranger")
#	return

#VARIABLE_CONFIG_KEYS

def get_configs_status(configs, s3_path, configs_file_prefix, variable_config_keys, data_dir):
	ls_cmd = 'aws s3 ls {} --recursive'.format(s3_path)
	files = os.popen(ls_cmd).read()
	latest_configs_file = None
	latest_version_num = 0
	is_new_version = False
	for f in files.rstrip().split('\n'):
		j = f.split('/')[-1].split('.')[0:-1]
		if ".".join(j[0:-1]) == configs_file_prefix:
			v = int(j[-1][1:])
			if v > latest_version_num:
				latest_version_num = v
				latest_configs_file = "/".join(f.split('/')[-2:])
	if latest_configs_file is None:
		version = 1
	else:
		cp_cmd = 'aws s3 cp {}/{} {}'.format(s3_path, latest_configs_file, data_dir)
		os.system(cp_cmd)
		configs_invariant = {i:configs[i] for i in configs if i not in variable_config_keys}
		with open(os.path.join(data_dir, latest_configs_file.split('/')[-1])) as f:
			latest_configs = json.loads(f.read().rstrip())
			latest_configs_invariant = {i:latest_configs[i] for i in latest_configs if i not in variable_config_keys}
			if configs_invariant == latest_configs_invariant:
				version = latest_version_num
			else:
				version = latest_version_num + 1
	if version > latest_version_num:
		is_new_version = True
	return [is_new_version,"v"+str(version)]

#variable_config_keys = VARIABLE_CONFIG_KEYS
def get_configs_version_alignment(configs, data_dir, configs_dir_remote, configs_file_remote_prefix, variable_config_keys):
	# This function checks if the configs file is using configs that were already used (while disregarding fields variable_config_keys) and are therefore documented on S3.
	# If this is the first time these configs are used then it creates a new configs version and uploads the new configs to S3.
	# Finally, the version of the configs is returned; to be used for stamping results files with the configs version.
	configs_dir_local = os.path.join(data_dir,"configs")
	os.system("mkdir -p " + configs_dir_local)
	cmd = 'aws s3 sync {0} {1} --exact-timestamps'.format(configs_dir_remote, configs_dir_local)
	add_to_log("get_configs_version_alignment: {0}".format(cmd), level = "debug")
	add_to_log("cmd: {0}".format(os.popen(cmd).read()), level = "debug")
	version = None
	max_version = 0
	configs_invariant = {i:configs[i] for i in configs if i not in variable_config_keys}
	for configs_file in glob.glob(os.path.join(configs_dir_local,"*.txt")):
		with open(configs_file) as f:
			f_configs = json.loads(f.read().rstrip())
		if configs_invariant == f_configs:
			version = configs_file.split('/')[-1][0:-4].split('.')[1]
			break
		version_num = int(configs_file.split('/')[-1][0:-4].split('.')[1][1:])
		if (version_num > max_version):
			max_version = version_num
	if version is None:
		version = "v{0}".format(max_version+1)
		configs_file = os.path.join(configs_dir_local,"{0}{1}.txt".format(configs_file_remote_prefix, version))
		with open(configs_file, 'w') as f:
			json.dump(configs_invariant, f)
		cmd = 'aws s3 sync {0} s3://immuneaging/processed/configs/ --exclude "*" --include {1}'.format(configs_dir_local,configs_file)
		add_to_log("Uploading configs_file to S3 : {0}".format(os.popen(cmd).read()), level = "debug")
	return version

def zipdir(path, ziph):
    for root, dirs, files in os.walk(path):
        for file in files:
            ziph.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), 
                os.path.join(path, '..')))


def read_immune_aging_sheet(sheet, output_fn=None, sheet_name=None):
    url = "https://docs.google.com/spreadsheets/d/1XC6DnTpdLjnsTMReGIeqY4sYWXViKke_cMwHwhbdxIY/gviz/tq?tqx=out:csv&sheet={}".format(
        sheet
    )

    # testing url of bad sample sheet
    # url = "https://docs.google.com/spreadsheets/d/1YO1HLGLnO3PPUiK1vKZd52yoCInwpLl60zoi4zxkOrE/gviz/tq?tqx=out:csv&sheet={}".format(
    #     sheet
    # )

    try:
        import gdown
    except ImportError as e:
        raise ImportError(
            "gdown is not installed. Please install gdown via: pip install gdown"
        )

    with warnings.catch_warnings(record=True) as w:
        warnings.filterwarnings("error")
        output_fn = gdown.download(url, output_fn, quiet=False)

        if len(w) == 1:
            warnings.showwarning(
                msg.message, msg.category, msg.filename, msg.lineno, msg.line
            )

    data = pd.read_csv(output_fn)
    os.remove(output_fn)
    return data