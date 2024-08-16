import os
import pymongo
import sys


def find_mongo_primary(auth):
    mongo_client = pymongo.MongoClient(f"mongodb://{auth}@mongodb-replicaset:27017/?replicaSet=rs0")
    topology = mongo_client.admin.command("ismaster")
    primary = topology.get("primary")
    return primary


def connect_to_mongo(connection_string, mode, tag):
    # Connect to MongoDB
    mongo_client = pymongo.MongoClient(connection_string)
    db = mongo_client['domino']
    collection = db['environment_revisions']

    # Build the query using the variable from the file
    query = {'metadata.dockerImageName.tag': tag}

    if mode == "find":
        print(f"Finding {tag}")
        # Execute the query
        results = collection.find(query)
        # Print the results
        for result in results:
            print(result)
    elif mode == "delete":
        print(f"Deleting {tag}")
        # Execute the query
        collection.delete_one(query)


def main():
    primary = find_mongo_primary(mongo_auth)
    print(primary)

    connection_string: str = "mongodb://" + mongo_auth + "@" + primary

    if work_mode != "find" and work_mode != "delete":
        print(f"Work mode {work_mode} not recognised")
        print("Try again with mongo_deletion2.py find|delete <filename>")
        exit(1)
    else:
        with open(filename, 'r') as file:
            while tag := file.readline().strip().split(":", 1)[0]:
                print(tag)
                connect_to_mongo(connection_string, work_mode, tag)
        exit(0)


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[2] == "help":
        print("Please provide a work mode and a file to read from")
        print("e.g. mongo_deletion2.py find|delete docker_tags.txt")
        exit(1)
    else:
        work_mode: str = sys.argv[1]
        filename: str = sys.argv[2]
    # filename: str = "to_delete.txt"

    if os.getenv("MONGO_PASSWORD") is not None:
        mongo_auth: str = "admin:" + os.getenv("MONGO_PASSWORD")
        print(mongo_auth)
    else:
        print("MONGO_PASSWORD variable not found")

    main()
