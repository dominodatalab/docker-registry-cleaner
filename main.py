import subprocess
import sys

def run_script(script_path, args):
    try:
        subprocess.run([sys.executable, script_path] + args, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running script {script_path}: {e}", file=sys.stderr)
        sys.exit(1)

def display_usage():
    print("Usage:")
    print("  main.py <script_keyword> [additional_args...]")
    print("  main.py <script_keyword> -h")
    print("\nScript Keywords and Examples:")
    print("  extract_metadata")
    print("    Description: Runs the extract-metadata.py script.")
    print("    Example: python main.py extract_metadata")
    print("\n  image_data_analysis")
    print("    Description: Runs the image-data-analysis.py script.")
    print("    Example: python main.py image_data_analysis --registry-url <url> --repository-name <name>")
    print("\n  inspect_workload")
    print("    Description: Runs the inspect-workload.py script.")
    print("    Example: python main.py inspect_workload")
    print("\n  security_scanning")
    print("    Description: Runs the security-scanning.py script.")
    print("    Example: python main.py security_scanning")
    print("\nNote:")
    print("  Use '-h' after a script keyword to display its usage instructions.")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ['-h', '--help']:
        display_usage()
        sys.exit(1)

    script_keyword = sys.argv[1]
    additional_args = sys.argv[2:]

    script_paths = {
        "extract_metadata": "metadata_extraction/extract-metadata.py",
        "image_data_analysis": "python/image-data-analysis.py",
        "inspect_workload": "python/inspect-workload.py",
        "security_scanning": "python/security-scanning.py"
    }

    script_path = script_paths.get(script_keyword)
    if not script_path:
        print(f"Unknown script keyword: {script_keyword}", file=sys.stderr)
        sys.exit(1)

    run_script(script_path, additional_args)

if __name__ == "__main__":
    main()
