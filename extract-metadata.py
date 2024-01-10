import os
import subprocess
import json
import re
import logging

def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_env_variable(var_name, default=None):
    return os.getenv(var_name, default)

def get_mongo_replicas(namespace):
    # Retrieve the number of MongoDB replicas in the specified namespace
    try:
        replicas = subprocess.getoutput(f"kubectl -n {namespace} get statefulset mongodb-replicaset -o jsonpath='{{.spec.replicas}}'")
        return int(replicas)
    except Exception as e:
        logging.error(f"Error getting replicas: {e}")
        return 0

def get_admin_auth(namespace):
    # Get the admin authentication credentials for MongoDB
    try:
        admin_auth_cmd = f"kubectl get secret -n {namespace} -o go-template='{{{{ printf \"%s:%s\" (.data.user | base64decode) (.data.password | base64decode) }}}}' mongodb-replicaset-admin"
        return subprocess.getoutput(admin_auth_cmd)
    except Exception as e:
        logging.error(f"Error getting admin authentication: {e}")
        return ""

def execute_mongo_script(namespace, host, opts, mongo_js, admin_auth):
    # Execute the MongoDB script using kubectl exec command
    try:
        mongo_cmd = f"kubectl exec -it -n {namespace} mongodb-replicaset-0 -c mongodb-replicaset -- mongo mongodb://{admin_auth}@{host}:27017/domino?{opts} < {mongo_js}"
        process = subprocess.Popen(mongo_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output, _ = process.communicate()
        return output.decode()
    except Exception as e:
        logging.error(f"Error executing MongoDB script: {e}")
        return ""

def process_output(output):
    data = []
    for line in output.split('\n'):
        if line.strip().startswith('{'):
            line = re.sub(r'ObjectId\("([^"]+)"\)', r'"\1"', line)
            line = re.sub(r'ISODate\("([^"]+)"\)', r'"\1"', line)
            try:
                record = json.loads(line)
                data.append(record)
            except json.JSONDecodeError as e:
                logging.warning(f"Skipping non-JSON line: {line}")
    return data


def write_to_file(data, filename):
    with open(filename, 'w') as file:
        headers = ["Author ID", "Created", "Repository", "Tag", "_id", "Project Name"]
        file.write(" | ".join(headers) + "\n")
        file.write("-" * 50 + "\n")
        for record in data:
            row = [
                str(record.get("authorId", "")),
                str(record.get("created", "")),
                record.get("repository", ""),
                record.get("tag", ""),
                str(record.get("_id", "")),
                record.get("projectName", "")
            ]
            file.write(" | ".join(row) + "\n")

def main():
    setup_logging()

    namespace = get_env_variable('NAMESPACE', 'domino-platform')
    host = "mongodb-replicaset-0.mongodb-replicaset"
    opts = "authSource=admin"
    mongo_js = "mongo.js"
    output_file = "output_table.txt"

    # Get replicas and admin authentication
    replicas = get_mongo_replicas(namespace)
    if replicas > 1:
        opts += "&replicaSet=rs0"
        host = ",".join([f"mongodb-replicaset-{i}.mongodb-replicaset" for i in range(replicas)])

    admin_auth = get_admin_auth(namespace)
    output = execute_mongo_script(namespace, host, opts, mongo_js, admin_auth)
    data = process_output(output)
    write_to_file(data, output_file)

if __name__ == "__main__":
    main()
