import argparse
import os
import subprocess
import logging
from pathlib import Path 


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


def execute_mongo_script(namespace, host, opts, mongo_js, admin_auth):
    results_dir = Path(__file__).resolve().parent.parent / 'results'
    os.makedirs(results_dir, exist_ok=True)

    output_file_name = Path(mongo_js).stem + "_output.json"
    output_file_path = results_dir / output_file_name

    mongo_cmd = f"kubectl exec -it -n {namespace} mongodb-replicaset-0 -c mongodb-replicaset -- mongo --quiet mongodb://{admin_auth}@{host}:27017/domino?{opts} < {mongo_js} 2>/dev/null"
    
    try:
        output_data = []
        process = subprocess.Popen(mongo_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in iter(process.stdout.readline, b''):
            if "WARNING: some history file lines were truncated" not in line.decode():
                output_data.append(line.decode())
        formatted_output = ''.join(output_data)
        with open(output_file_path, 'w') as file:
            file.write(formatted_output)
    except Exception as e:
        logging.error(f"Error executing MongoDB script: {e}")


def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description='Run MongoDB scripts within a Kubernetes environment.',
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        'script',
        nargs='?',
        default=None,
        help='Path to the MongoDB script to run (*.js). If not specified, default scripts will be run.\n'
             'Example usage:\n'
             '  python extract-metadata.py mongo.js  # Runs a specified script\n'
             '  python extract-metadata.py           # Runs default scripts (model and workspace metadata)'
    )

    args = parser.parse_args() 

    if args.script and not os.path.isfile(args.script):
        logging.error(f"The specified script file does not exist: {args.script}")
        return  
    
    namespace = get_env_variable('NAMESPACE', 'domino-platform')
    host = "mongodb-replicaset-0.mongodb-replicaset"
    opts = "authSource=admin"
    admin_auth = get_admin_auth(namespace)
    replicas = get_mongo_replicas(namespace)

    if replicas > 1:
        opts += "&replicaSet=rs0"
        host = ",".join([f"mongodb-replicaset-{i}.mongodb-replicaset" for i in range(replicas)])

    if args.script:
        execute_mongo_script(namespace, host, opts, args.script, admin_auth)
    else:
        script_details = {
            'model': ("metadata_extraction/model_env_usage.js", "model_output.json"),
            'workspace': ("metadata_extraction/workspace_env_usage.js", "workspace_output.json")
        }
        for key, value in script_details.items():
            mongo_js, _ = value
            execute_mongo_script(namespace, host, opts, mongo_js, admin_auth)


if __name__ == "__main__":
    main()
