#!/usr/bin/env bash

set -ex

namespace="domino-platform"
#namespace=${NAMESPACE:-}

print_usage() {
  echo "Usage: NAMESPACE=domino-platform $0"
  echo "Please provide the platform namespace in a NAMESPACE environment variable."
}

if [[ -z "$namespace" ]]; then
  print_usage
  exit 1
fi

script_dir="$(dirname "$(readlink -f "$0")")"
HOST="mongodb-replicaset-0.mongodb-replicaset"
replicas=$(kubectl -n "$namespace" get statefulset mongodb-replicaset -o jsonpath='{.spec.replicas}')
OPTS="authSource=admin"

if [ $replicas -gt 1 ]; then
  OPTS="$OPTS&replicaSet=rs0"
  for i in $(seq 2 $replicas); do
    HOST="$HOST,mongodb-replicaset-$(($i-1)).mongodb-replicaset";
  done
fi

mongo_hostname=$(echo -n mongodb-replicaset-0.mongodb-replicaset && for i in $(seq 2 $replicas); do echo -n ,mongodb-replicaset-$(($i-1)).mongodb-replicaset; done; echo)

set +x

admin_auth=$(kubectl get secret -n "$namespace" -o go-template='{{ printf "%s:%s" (.data.user | base64decode) (.data.password | base64decode) }}' mongodb-replicaset-admin)

kubectl exec -it -n $namespace mongodb-replicaset-0 -c mongodb-replicaset -- mongo mongodb://$admin_auth@$HOST:27017/domino?$OPTS < mongo.js | tail -n +6 | grep -v "bye" | sed 's/.*"repository" : "\(.*\)", "tag" : "\(.*\)".*/\1 \2/' > output.txt
#kubectl exec -it -n $namespace mongodb-replicaset-0 -c mongodb-replicaset -- mongo mongodb://$admin_auth@$HOST:27017/domino?$OPTS < mongo.js | tail -n +6 | grep -v "bye" > output.txt

set -e

if ! command -v aws &> /dev/null
then
    echo "AWS CLI not found."
fi

while read -r line; do
  repo=$(echo "$line" | cut -d ' ' -f 1)
  image=$(echo "$line" | cut -d ' ' -f 2)
  id=$(echo "$line" | cut -d ' ' -f 3)
  echo "Do you want to delete image tagged: $image from repository $repo? (y/n)"
  read -ru 3 answer
  if [ "$answer" = "y" ]; then
    echo "Deleting image $image from repository $repo..."
    #aws ecr batch-delete-image --repository-name "$repo" --image-ids imageDigest="$image"
    kubectl exec -it -n $namespace mongodb-replicaset-0 -c mongodb-replicaset -- mongo mongodb://$admin_auth@$HOST:27017/domino?$OPTS < delete_image.js "$id"
  else
    echo "Skipping image $image from repository $repo."
  fi
done 3< "/dev/tty" < "output.txt"
