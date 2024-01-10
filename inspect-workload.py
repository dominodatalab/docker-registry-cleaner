import subprocess
import json
from collections import defaultdict
from tabulate import tabulate

def get_running_pods(namespace, prefix=None):
    command = f"kubectl get pods -n {namespace} -o custom-columns=:metadata.name"
    output = subprocess.check_output(command, shell=True)
    pods = output.decode().splitlines()

    if prefix:
        return [pod for pod in pods if pod.startswith(prefix)]
    else:
        return pods

def get_container_images(pod_name, namespace):
    command = f"kubectl get pod {pod_name} -n {namespace} -o jsonpath={{.spec.containers[*].image}}"
    output = subprocess.check_output(command, shell=True)
    return output.decode().split()

def get_pod_labels(pod_name, namespace):
    command = f"kubectl get pod {pod_name} -n {namespace} -o jsonpath={{.metadata.labels}}"
    output = subprocess.check_output(command, shell=True)
    labels_str = output.decode()
    return json.loads(labels_str) if labels_str else {}

def filter_images_by_registry(images, registry):
    return [image for image in images if registry in image]

def remove_prefix(tag, prefix):
    return tag.replace(prefix, "").split(":")[1]

def save_to_file(data, output_file):
    with open(output_file, "w") as file:
        file.write(data)

def main():
    registry_url = "946429944765.dkr.ecr.us-west-2.amazonaws.com/stevel3358"
    prefix_to_remove = "946429944765.dkr.ecr.us-west-2.amazonaws.com/stevel3358/"
    target_namespace = "domino-compute"
    pod_name_prefixes = ["model-", "run-"]
    output_file = "workload-report.txt"

    image_tags = defaultdict(lambda: {'pods': set(), 'count': 0, 'labels': []})

    for prefix in pod_name_prefixes:
        running_pods = get_running_pods(target_namespace, prefix)

        for prefix in pod_name_prefixes:
            running_pods = get_running_pods(target_namespace, prefix)
        
            for pod in running_pods:
                try:
                    images = get_container_images(pod, target_namespace)
                    filtered_images = filter_images_by_registry(images, registry_url)
        
                    for image in filtered_images:
                        clean_tag = remove_prefix(image, prefix_to_remove)
        
                        if clean_tag not in image_tags:
                            image_tags[clean_tag] = {
                                'pods': set(),
                                'count': 0,
                                'labels': []
                            }
        
                        if pod not in image_tags[clean_tag]['pods']:
                            image_tags[clean_tag]['pods'].add(pod)
                            image_tags[clean_tag]['count'] += 1
        
                            labels = get_pod_labels(pod, target_namespace)
                            label_info = {
                                'Pod Name': pod,
                                'dominodatalab.com/project-name': labels.get('dominodatalab.com/project-name', ''),
                                'dominodatalab.com/project-owner-username': labels.get('dominodatalab.com/project-owner-username', ''),
                                'dominodatalab.com/workload-type': labels.get('dominodatalab.com/workload-type', '')
                            }
        
                            if label_info not in image_tags[clean_tag]['labels']:
                                image_tags[clean_tag]['labels'].append(label_info)
        
                except subprocess.CalledProcessError as e:
                    print(f"Error retrieving container images for {pod}: {e}")

    headers = ["Tag", "Pod Names", "Count", "Pod Labels"]
    rows = []

    for tag, info in image_tags.items():
        pod_names = ', '.join(info['pods'])
        count = info['count']
        labels_str = json.dumps(info['labels'], indent=2)
        rows.append([tag, pod_names, count, labels_str])

    table = tabulate(rows, headers=headers, tablefmt="grid")
    print(table)

    save_to_file(table, output_file)
    print(f"Results saved to {output_file}")
if __name__ == "__main__":
    main()

