package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func request(t *testing.T, server *httptest.Server, method, path, body string) (int, message) {
	t.Helper()
	r, _ := http.NewRequest(method, server.URL+path, strings.NewReader(body))
	r.Header.Set("Authorization", "Bearer client-test")
	r.Header.Set("Idempotency-Key", "stable-key")
	r.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var m message
	if json.NewDecoder(resp.Body).Decode(&m) != nil {
		t.Fatal("non-JSON response")
	}
	return resp.StatusCode, m
}

func TestAuthenticationAndOffline(t *testing.T) {
	g := newGateway("client-test", "worker-test", "example-worker")
	s := httptest.NewServer(g.handler())
	defer s.Close()
	resp, err := http.Get(s.URL + "/v1/health")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Fatal(resp.StatusCode)
	}
	code, m := request(t, s, "GET", "/v1/health", "")
	if code != 503 || m["ready"] != false {
		t.Fatal(code, m)
	}
}

func TestWorkerRoutingAndJobs(t *testing.T) {
	g := newGateway("client-test", "worker-test", "example-worker")
	s := httptest.NewServer(g.handler())
	defer s.Close()
	head := http.Header{"Authorization": []string{"Bearer worker-test"}, "X-Worker-ID": []string{"example-worker"}}
	c, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(s.URL, "http")+"/v1/worker/ws", head)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.WriteJSON(message{"type": "worker.hello", "protocol": "1", "worker_id": "example-worker", "capabilities": message{"rpc": true, "durable_jobs": true, "correlated_acks": true}})
	var submissions atomic.Int32
	go func() {
		jobs := make(map[string]string)
		previousRequest := ""
		for {
			var m message
			if c.ReadJSON(&m) != nil {
				return
			}
			switch str(m, "type") {
			case "worker.health":
				_ = c.WriteJSON(message{"request_id": m["request_id"], "ready": true, "worker_id": "example-worker"})
			case "job.submit":
				job := message(m["job"].(map[string]any))
				id := str(job, "job_id")
				b, _ := json.Marshal(job)
				hash := sha256.Sum256(b)
				digest := hex.EncodeToString(hash[:])
				if old, ok := jobs[id]; ok && old != digest {
					_ = c.WriteJSON(message{"type": "job.accepted", "job_id": id, "request_id": previousRequest})
					_ = c.WriteJSON(message{"type": "job.rejected", "job_id": id, "error": "idempotency conflict", "request_id": m["request_id"]})
					continue
				}
				if _, ok := jobs[id]; !ok {
					submissions.Add(1)
					jobs[id] = digest
				}
				_ = c.WriteJSON(message{"type": "job.accepted", "job_id": id, "request_id": m["request_id"]})
				previousRequest = str(m, "request_id")
			case "job.query":
				_ = c.WriteJSON(message{"type": "rpc.result", "request_id": m["request_id"], "status": "failed", "error": "CUDA out of memory"})
			}
		}
	}()
	deadline := time.Now().Add(time.Second)
	for {
		g.mu.Lock()
		connected := g.active != nil
		g.mu.Unlock()
		if connected {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("worker not registered")
		}
		time.Sleep(time.Millisecond)
	}
	code, m := request(t, s, "GET", "/v1/health", "")
	if code != 200 || m["ready"] != true {
		t.Fatal(code, m)
	}
	body := `{"text":"你好","role":"讲述者","emotion":"平静","voice_sha256":"` + strings.Repeat("a", 64) + `"}`
	code, m = request(t, s, "POST", "/v1/tts/jobs", body)
	id := str(m, "id")
	if code != 202 || !safeID.MatchString(id) {
		t.Fatal(code, m)
	}
	code, m = request(t, s, "POST", "/v1/tts/jobs", body)
	if code != 202 || str(m, "id") != id || submissions.Load() != 1 {
		t.Fatal(code, m, submissions.Load())
	}
	code, m = request(t, s, "POST", "/v1/tts/jobs", strings.Replace(body, "你好", "不同", 1))
	if code != 409 {
		t.Fatal(code, m)
	}
	code, m = request(t, s, "GET", "/v1/tts/jobs/"+id, "")
	if code != 200 || str(m, "status") != "failed" || str(m, "error") != "CUDA out of memory" {
		t.Fatal(code, m)
	}
}

func TestRejectMalformedAudio(t *testing.T) {
	w := httptest.NewRecorder()
	wavResponse(w, message{"audio_base64": base64.StdEncoding.EncodeToString([]byte("HTML"))})
	if w.Code != 502 {
		t.Fatal(w.Code)
	}
}

func TestSyncRejectsNonMultipartBody(t *testing.T) {
	g := newGateway("client-test", "worker-test", "example-worker")
	r := httptest.NewRequest("POST", "/v1/voices/sync", bytes.NewBufferString("not a ZIP"))
	r.Header.Set("Content-Type", "application/octet-stream")
	w := httptest.NewRecorder()
	g.syncVoices(w, r)
	if w.Code != 400 {
		t.Fatal(w.Code)
	}
	_, _ = io.Copy(io.Discard, w.Result().Body)
}
