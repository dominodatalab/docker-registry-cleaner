import argparse
import os
import subprocess
import logging

def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_env_variable(var_name, default=None):
    return os.getenv(var_name, default)

def run_command(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode().strip()
    except subprocess.CalledProcessError as e:
        logging.error(f"Command '{cmd}' failed with error: {e.output.decode().strip()}")
        return ""

def get_mongo_replicas(namespace):
    cmd = f"kubectl -n {namespace} get statefulset mongodb-replicaset -o jsonpath='{{.spec.replicas}}'"
    replicas = run_command(cmd)
    return int(replicas) if replicas.isdigit() else 0

def get_admin_auth(namespace):
    cmd = f"kubectl get secret -n {namespace} -o go-template=\"{{{{ printf \\\"%s:%s\\\" (.data.user | base64decode) (.data.password | base64decode) }}}}\" mongodb-replicaset-admin"
    return run_command(cmd)

def execute_mongo_script(namespace, host, opts, mongo_js, output_file, admin_auth):
    mongo_cmd = f"kubectl exec -it -n {namespace} mongodb-replicaset-0 -c mongodb-replicaset -- mongo --quiet mongodb://{admin_auth}@{host}:27017/domino?{opts} < {mongo_js}"
    try:
        with open(output_file, 'w') as file:
            process = subprocess.Popen(mongo_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in iter(process.stdout.readline, b''):
                if "WARNING: some history file lines were truncated" not in line.decode():
                    file.write(line.decode())
    except Exception as e:
        logging.error(f"Error executing MongoDB script: {e}")

def main():
    setup_logging()

    parser = argparse.ArgumentParser(description='Run MongoDB scripts based on parameters.')
    parser.add_argument('type', nargs='?', default=None, help='Type of script to run (model, workspace, or both)')
    args = parser.parse_args()

    namespace = get_env_variable('NAMESPACE', 'domino-platform')
    host = "mongodb-replicaset-0.mongodb-replicaset"
    opts = "authSource=admin"

    admin_auth = get_admin_auth(namespace)
    replicas = get_mongo_replicas(namespace)
    if replicas > 1:
        opts += "&replicaSet=rs0"
        host = ",".join([f"mongodb-replicaset-{i}.mongodb-replicaset" for i in range(replicas)])

    script_details = {
        'model': ("model_env_usage.js", "model_output.json"),
        'workspace': ("workspace_env_usage.js", "workspace_output.json")
    }

    script_to_run = script_details.get(args.type, None)
    if script_to_run or args.type is None:
        for key, value in script_details.items():
            mongo_js, output_file = value
            execute_mongo_script(namespace, host, opts, mongo_js, output_file, admin_auth)

if __name__ == "__main__":
    main()
