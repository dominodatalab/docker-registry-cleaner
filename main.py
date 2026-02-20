


def print_help():
    print("Usage: python docker-registry-cleaner [options]")
    print("Options:")
    print("  --delete|find  Sets the mode docker-registry-cleaner will work in.")
    print("      Do you want to find images to delete, or delete them?")
    print("  --unused-layers  Find or delete Docker image layers that are unused in Domino")
    print("  --unused-references  Find or delete references in MongoDB to non-existent Docker images")
    print("  --file  Write found objects to a file or read objects to delete from a file.")
    print("  --plan (default)  Plan the changes docker-registry-cleaner will make. Nothing will be deleted.")
    print("  --apply  Apply the planned changes")



if __name__ == "__main__":
    main()
