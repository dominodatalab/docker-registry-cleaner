package main

import (
	"fmt"
	"log"
)

func main() {
	tags, err := readTags("stevel33582", "environment")
	if err != nil {
		log.Fatal(err)
		return
	}
	fmt.Println(tags)
	meta, err := readImageMetadata("stevel33582", "environment", tags[0])
	if err != nil {
		log.Fatal(err)
		return
	}
	fmt.Println(meta)
}
