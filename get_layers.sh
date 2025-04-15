#!/bin/bash

# Set the registry URL and repository name
registry_url="946429944765.dkr.ecr.us-west-2.amazonaws.com"
repository_name="stevel33582"

# Function to get image information
get_image_info() {
    local image=$1
    local output=$(kubectl exec -n domino-platform skopeo -- skopeo list-tags docker://${registry_url}/${repository_name}/${image} 2>/dev/null)

    if [ $? -eq 0 ]; then
        tags=$(echo "${output}" | jq -r '.Tags[]' 2>/dev/null)

        for tag in $tags; do
            echo "tag: ${tag}"
                layer_ids=$(kubectl exec -n domino-platform skopeo -- skopeo inspect docker://${registry_url}/${repository_name}/${image}:${tag} 2>/dev/null | jq -r '.LayersData[].Digest')
                for layer_id in $layer_ids; do
                    layer_size=$(kubectl exec -n domino-platform skopeo -- skopeo inspect docker://${registry_url}/${repository_name}/${image}:${tag} 2>/dev/null | jq --arg layer_id "${layer_id}" '.LayersData[] | select(.Digest == $layer_id).Size')
                    echo "  - Layer: ${layer_id}"
                    echo "    Size: ${layer_size} bytes"
                done
        done

        echo "----------------------------------"
    else
        echo "Failed to retrieve information for image: ${image}"
        echo "----------------------------------"
    fi
}

# Get a list of images from the repository
images=("environment" "model")


# Loop through each image and get its information
while IFS= read -r image; do
    get_image_info "${image}"
done <<< "${images}"