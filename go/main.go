package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"slices"
	"sort"
	"strings"
	"time"
)

type SkopeoImageData struct {
	Name          string            `json:"Name"`
	Digest        string            `json:"Digest"`
	RepoTags      []string          `json:"RepoTags"`
	Created       time.Time         `json:"Created"`
	DockerVersion string            `json:"DockerVersion"`
	Labels        map[string]string `json:"Labels"`
	Architecture  string            `json:"Architecture"`
	Os            string            `json:"Os"`
	Layers        []string          `json:"Layers"`
	LayersData    []SkopeoLayerData `json:"LayersData"`
	Env           []string          `json:"Env"`
}

type SkopeoLayerData struct {
	MIMEType    string      `json:"MIMEType"`
	Digest      string      `json:"Digest"`
	Size        int         `json:"Size"`
	Annotations interface{} `json:"Annotations"`
}

type Mode int

const (
	LayersToImages Mode = iota
	ImagesToLayers
)

type LayerInfo struct {
	Digest    string      `json:"digest"`
	Size      int         `json:"size"`
	Frequency int         `json:"frequency"`
	Images    []ImageInfo `json:"images,omitempty"`
}

type ImageInfo struct {
	Tag    string      `json:"tag"`
	Layers []LayerInfo `json:"layers,omitempty"`
}

type LayersToImagesOut struct {
	Layers []LayerInfo `json:"layers"`
}

type ImagesToLayersOut struct {
	Images []ImageInfo `json:"images"`
}

// tag -> image data
var allImages = make(map[string]SkopeoImageData)

// digest -> layer data
var allLayers = make(map[string]SkopeoLayerData)

// layer digest -> image tag[]
var layersUse = make(map[string][]string)

var dockerAddress string
var envName string
var repoNames = []string{"environment", "model"}

func main() {
	if len(os.Args) != 4 {
		printHelpAndExit()
	}

	var mode Mode
	switch os.Args[1] {
	case "layers":
		mode = LayersToImages
	case "images":
		mode = ImagesToLayers
	default:
		log.Fatalf("Invalid option: %s; must be 'layers' or 'images'", os.Args[1])
	}
	dockerAddress = os.Args[2]
	envName = os.Args[3]

	for _, repoName := range repoNames {
		processRepo(repoName)
	}

	var out any

	if mode == LayersToImages {
		layers := make([]LayerInfo, len(allLayers))
		layerIndex := 0
		for digest, layerData := range allLayers {
			images := make([]ImageInfo, len(layersUse[digest]))
			imageIndex := 0
			for _, tag := range layersUse[digest] {
				images[imageIndex] = ImageInfo{
					Tag: tag,
				}
				imageIndex++
			}
			layers[layerIndex] = LayerInfo{
				Digest:    digest,
				Size:      layerData.Size,
				Frequency: len(layersUse[digest]),
				Images:    images,
			}
			layerIndex++
		}
		sortLayers(&layers)
		out = LayersToImagesOut{layers}
	}

	if mode == ImagesToLayers {
		images := make([]ImageInfo, len(allImages))
		imageIndex := 0
		for tag, imageData := range allImages {
			layers := make([]LayerInfo, len(imageData.Layers))
			layerIndex := 0
			// Note that an image may contain multiple layers with the same digest.
			for _, digest := range imageData.Layers {
				layers[layerIndex] = LayerInfo{
					Digest:    digest,
					Size:      allLayers[digest].Size,
					Frequency: len(layersUse[digest]),
				}
				layerIndex++
			}
			images[imageIndex] = ImageInfo{
				Tag:    tag,
				Layers: layers,
			}
			imageIndex++
		}
		out = ImagesToLayersOut{images}
	}

	var jsonBytes, err = json.MarshalIndent(out, "", "    ")
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(string(jsonBytes))
}

func printHelpAndExit() {
	exeName := strings.Split(os.Args[0], "/")
	_, _ = fmt.Fprintf(os.Stderr, `Domino image analyzer.
	
	    Usage:
	        %[1]s layers|tags <docker-registry-address> <domino-environment>
	
	    Where,
	        layers|images               - Query mode: layers (to images) or images (to layers)
	        <docker-registry-address>   - e.g., 946429944765.dkr.ecr.us-west-2.amazonaws.com
	        <domino-environment>        - e.g., stevel33582
	`,
		exeName[len(exeName)-1])
	os.Exit(2)
}

func sortLayers(layers *[]LayerInfo) {
	sort.Slice(*layers,
		func(i, j int) bool {
			if (*layers)[i].Frequency == (*layers)[j].Frequency {
				return (*layers)[i].Size > (*layers)[j].Size
			} else {
				return (*layers)[i].Frequency > (*layers)[j].Frequency
			}
		})
}

func processRepo(repoName string) {
	tags, err := readTags(repoName)
	if err != nil {
		log.Fatal(err)
	}
	for _, tag := range tags {
		processImage(repoName, tag)
	}
}

func processImage(repoName string, tag string) {
	image, err := readImageData(repoName, tag)
	if err != nil {
		log.Fatal(err)
	}
	allImages[tag] = image
	for _, layer := range image.LayersData {
		digest := layer.Digest
		_, ok := allLayers[digest]
		if !ok {
			allLayers[digest] = layer
		}
		_, ok = layersUse[digest]
		if !ok {
			layersUse[digest] = []string{tag}
		} else if slices.Index(layersUse[digest], tag) == -1 {
			layersUse[digest] = append(layersUse[digest], tag)
		}
	}

}
