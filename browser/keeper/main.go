// Command livellm-keeper is the browser pod's control sidecar.
package main

import (
	"log"
	"net/http"
)

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	log.Fatal(http.ListenAndServe(":9300", mux))
}
