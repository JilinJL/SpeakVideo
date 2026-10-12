// SpeakVideo's authenticated, memory-only HTTP to Windows WSS bridge.
package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

const maxBody = 32 << 20
const maxFrame = 96 << 20

var safeName = regexp.MustCompile(`^[\p{L}\p{N}_-]{1,64}$`)
var safeID = regexp.MustCompile(`^j_[a-f0-9]{64}$`)

type message map[string]any

type worker struct {
	conn  *websocket.Conn
	write sync.Mutex
	done  chan struct{}
}

func (w *worker) send(m message) error {
	w.write.Lock()
	defer w.write.Unlock()
	_ = w.conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
	return w.conn.WriteJSON(m)
}

type gateway struct {
	clientToken, workerToken, workerID string
	mu                                 sync.Mutex
	active                             *worker
	waiters                            map[string]chan message
	submitMu                           sync.Mutex
	slots                              chan struct{}
}

func newGateway(client, token, id string) *gateway {
	return &gateway{clientToken: client, workerToken: token, workerID: id,
		waiters: make(map[string]chan message), slots: make(chan struct{}, 8)}
}

func str(m message, key string) string { v, _ := m[key].(string); return v }
func identifier() string               { var b [16]byte; _, _ = rand.Read(b[:]); return hex.EncodeToString(b[:]) }
func auth(r *http.Request, token string) bool {
	return token != "" && subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+token)) == 1
}
func reply(w http.ResponseWriter, code int, data any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(data)
}
func failure(w http.ResponseWriter, code int, err error) {
	reply(w, code, message{"detail": err.Error()})
}

func (g *gateway) websocket(w http.ResponseWriter, r *http.Request) {
	if !auth(r, g.workerToken) || r.Header.Get("X-Worker-ID") != g.workerID {
		failure(w, 401, errors.New("unauthorized worker"))
		return
	}
	up := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return r.Header.Get("Origin") == "" }}
	c, err := up.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer c.Close()
	c.SetReadLimit(maxFrame)
	_ = c.SetReadDeadline(time.Now().Add(15 * time.Second))
	var hello message
	if c.ReadJSON(&hello) != nil || str(hello, "type") != "worker.hello" || str(hello, "worker_id") != g.workerID || str(hello, "protocol") != "1" {
		return
	}
	caps, _ := hello["capabilities"].(map[string]any)
	if caps["durable_jobs"] != true || caps["rpc"] != true || caps["correlated_acks"] != true {
		_ = c.WriteJSON(message{"type": "worker.error", "error": "SpeakVideo worker extension required"})
		return
	}
	wk := &worker{conn: c, done: make(chan struct{})}
	g.mu.Lock()
	// Never replace an active worker implicitly (avoids two independent GPU queues).
	if g.active != nil {
		g.mu.Unlock()
		return
	}
	g.active = wk
	g.mu.Unlock()
	log.Printf("worker connected id=%s", g.workerID)
	defer func() {
		g.mu.Lock()
		if g.active == wk {
			g.active = nil
		}
		close(wk.done)
		g.mu.Unlock()
		log.Printf("worker disconnected id=%s", g.workerID)
	}()
	_ = c.SetReadDeadline(time.Now().Add(70 * time.Second))
	c.SetPongHandler(func(string) error { return c.SetReadDeadline(time.Now().Add(70 * time.Second)) })
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	go func() {
		t := time.NewTicker(20 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				if c.WriteControl(websocket.PingMessage, nil, time.Now().Add(10*time.Second)) != nil {
					_ = c.Close()
					return
				}
			}
		}
	}()
	for {
		var event message
		if err := c.ReadJSON(&event); err != nil {
			return
		}
		_ = c.SetReadDeadline(time.Now().Add(70 * time.Second))
		key := str(event, "request_id")
		kind := str(event, "type")
		if kind == "voice.sync.result" {
			key = "sync:" + str(event, "sync_id")
		}
		g.mu.Lock()
		ch := g.waiters[key]
		g.mu.Unlock()
		if ch != nil {
			select {
			case ch <- event:
			default:
			}
		}
	}
}

func (g *gateway) call(ctx context.Context, key string, m message) (message, error) {
	g.mu.Lock()
	wk := g.active
	if wk == nil {
		g.mu.Unlock()
		return nil, errors.New("Windows worker offline")
	}
	ch := make(chan message, 1)
	if _, exists := g.waiters[key]; exists {
		g.mu.Unlock()
		return nil, errors.New("request already pending")
	}
	g.waiters[key] = ch
	g.mu.Unlock()
	defer func() { g.mu.Lock(); delete(g.waiters, key); g.mu.Unlock() }()
	if err := wk.send(m); err != nil {
		return nil, errors.New("worker send failed")
	}
	select {
	case response := <-ch:
		return response, nil
	case <-wk.done:
		return nil, errors.New("worker disconnected; resume using the same job ID")
	case <-ctx.Done():
		return nil, errors.New("worker request timed out; resume using the same job ID")
	}
}

func (g *gateway) rpc(r *http.Request, kind string, data message, timeout time.Duration) (message, error) {
	id := identifier()
	data["type"] = kind
	data["request_id"] = id
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()
	result, err := g.call(ctx, id, data)
	if err == nil && str(result, "rpc_error") != "" {
		err = errors.New(str(result, "rpc_error"))
	}
	return result, err
}

func readJSON(w http.ResponseWriter, r *http.Request) (message, error) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	var data message
	dec := json.NewDecoder(r.Body)
	err := dec.Decode(&data)
	if err == nil && data == nil {
		err = errors.New("JSON object required")
	}
	if err == nil {
		var extra any
		if dec.Decode(&extra) != io.EOF {
			err = errors.New("trailing JSON")
		}
	}
	return data, err
}

func wavResponse(w http.ResponseWriter, m message) {
	data, err := base64.StdEncoding.DecodeString(str(m, "audio_base64"))
	if err != nil || len(data) < 44 || string(data[:4]) != "RIFF" || string(data[8:12]) != "WAVE" || len(data) > 64<<20 {
		failure(w, 502, errors.New("worker returned invalid WAV"))
		return
	}
	w.Header().Set("Content-Type", "audio/wav")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	_, _ = w.Write(data)
}

func (g *gateway) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/worker/ws", g.websocket)
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		m, err := g.rpc(r, "worker.health", message{}, 10*time.Second)
		if err != nil {
			reply(w, 503, message{"ready": false, "worker_id": g.workerID, "detail": err.Error()})
			return
		}
		code := 200
		if m["ready"] != true {
			code = 503
		}
		delete(m, "request_id")
		delete(m, "type")
		reply(w, code, m)
	})
	mux.HandleFunc("GET /v1/voices", func(w http.ResponseWriter, r *http.Request) {
		m, err := g.rpc(r, "voice.list", message{}, 30*time.Second)
		if err != nil {
			failure(w, 502, err)
			return
		}
		reply(w, 200, m["result"])
	})
	mux.HandleFunc("GET /v1/voices/download", func(w http.ResponseWriter, r *http.Request) {
		role, emotion := r.URL.Query().Get("role"), r.URL.Query().Get("emotion")
		if !safeName.MatchString(role) || !safeName.MatchString(emotion) {
			failure(w, 400, errors.New("invalid voice name"))
			return
		}
		m, err := g.rpc(r, "voice.download", message{"role": role, "emotion": emotion}, 30*time.Second)
		if err != nil {
			failure(w, 502, err)
			return
		}
		data, err := base64.StdEncoding.DecodeString(str(m, "content_base64"))
		if err != nil || len(data) == 0 || len(data) > maxBody {
			failure(w, 502, errors.New("invalid PT response"))
			return
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		_, _ = w.Write(data)
	})
	mux.HandleFunc("POST /v1/voices/sync", g.syncVoices)
	mux.HandleFunc("POST /v1/voices/create", func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
		reader, err := r.MultipartReader()
		if err != nil {
			failure(w, 400, err)
			return
		}
		var metadata message
		var content []byte
		filename := "reference.wav"
		for {
			part, e := reader.NextPart()
			if e == io.EOF {
				break
			}
			if e != nil {
				failure(w, 400, e)
				return
			}
			b, e := io.ReadAll(part)
			if e != nil {
				failure(w, 400, e)
				return
			}
			switch part.FormName() {
			case "metadata":
				if json.Unmarshal(b, &metadata) != nil {
					failure(w, 400, errors.New("invalid metadata"))
					return
				}
			case "file":
				content = b
				filename = part.FileName()
			}
		}
		if metadata == nil || len(content) == 0 || !safeName.MatchString(str(metadata, "role")) || !safeName.MatchString(str(metadata, "emotion")) || strings.TrimSpace(str(metadata, "ref_text")) == "" {
			failure(w, 400, errors.New("reference, voice name and ref_text required"))
			return
		}
		m, err := g.rpc(r, "voice.create", message{"metadata": metadata, "filename": filename, "content_base64": base64.StdEncoding.EncodeToString(content)}, time.Hour)
		if err != nil {
			failure(w, 502, err)
			return
		}
		reply(w, 200, m["result"])
	})
	mux.HandleFunc("POST /v1/voices/design", func(w http.ResponseWriter, r *http.Request) {
		data, err := readJSON(w, r)
		if err != nil {
			failure(w, 400, err)
			return
		}
		m, err := g.rpc(r, "voice.design", message{"payload": data}, time.Hour)
		if err != nil {
			failure(w, 502, err)
			return
		}
		wavResponse(w, m)
	})
	mux.HandleFunc("POST /v1/tts/jobs", func(w http.ResponseWriter, r *http.Request) {
		key := r.Header.Get("Idempotency-Key")
		if len(key) == 0 || len(key) > 512 {
			failure(w, 400, errors.New("Idempotency-Key required (max 512 bytes)"))
			return
		}
		data, err := readJSON(w, r)
		if err != nil {
			failure(w, 400, err)
			return
		}
		if strings.TrimSpace(str(data, "text")) == "" || len([]rune(str(data, "text"))) > 10000 || !safeName.MatchString(str(data, "role")) || !safeName.MatchString(str(data, "emotion")) || len(str(data, "voice_sha256")) != 64 {
			failure(w, 400, errors.New("invalid job parameters"))
			return
		}
		digest := sha256.Sum256([]byte(g.workerID + "\x00" + key))
		id := "j_" + hex.EncodeToString(digest[:])
		data["job_id"] = id
		g.submitMu.Lock()
		defer g.submitMu.Unlock()
		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()
		requestID := identifier()
		m, err := g.call(ctx, requestID, message{"type": "job.submit", "job": data, "request_id": requestID})
		if err != nil {
			failure(w, 503, err)
			return
		}
		if str(m, "type") == "job.rejected" {
			failure(w, 409, errors.New(str(m, "error")))
			return
		}
		reply(w, 202, message{"id": id})
	})
	mux.HandleFunc("GET /v1/tts/jobs/{id}", g.jobStatus)
	mux.HandleFunc("GET /v1/tts/jobs/{id}/audio", g.jobStatus)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.URL.Path == "/v1/worker/ws" {
			mux.ServeHTTP(w, r)
			return
		}
		if !auth(r, g.clientToken) {
			failure(w, 401, errors.New("unauthorized client"))
			return
		}
		if id := r.Header.Get("X-Worker-ID"); id != "" && id != g.workerID {
			failure(w, 404, errors.New("unknown worker"))
			return
		}
		select {
		case g.slots <- struct{}{}:
			defer func() { <-g.slots }()
		default:
			failure(w, 429, errors.New("too many concurrent requests"))
			return
		}
		mux.ServeHTTP(w, r)
	})
}

func (g *gateway) jobStatus(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !safeID.MatchString(id) {
		failure(w, 400, errors.New("invalid job ID"))
		return
	}
	audio := strings.HasSuffix(r.URL.Path, "/audio")
	m, err := g.rpc(r, "job.query", message{"job_id": id, "include_audio": audio}, 30*time.Second)
	if err != nil {
		failure(w, 502, err)
		return
	}
	if str(m, "status") == "missing" {
		failure(w, 404, errors.New("unknown job"))
		return
	}
	if audio {
		if str(m, "status") != "succeeded" {
			failure(w, 409, errors.New("job audio unavailable: "+str(m, "status")))
			return
		}
		wavResponse(w, m)
		return
	}
	out := message{"id": id, "status": m["status"]}
	if m["error"] != nil {
		out["error"] = m["error"]
	}
	if str(m, "status") == "succeeded" {
		out["audio_url"] = "/v1/tts/jobs/" + id + "/audio"
	}
	reply(w, 200, out)
}

func (g *gateway) syncVoices(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	reader, err := r.MultipartReader()
	if err != nil {
		failure(w, 400, err)
		return
	}
	var bundle []byte
	for {
		part, e := reader.NextPart()
		if e == io.EOF {
			break
		}
		if e != nil {
			failure(w, 400, e)
			return
		}
		if part.FormName() != "bundle" {
			_, _ = io.Copy(io.Discard, part)
			continue
		}
		bundle, e = io.ReadAll(part)
		if e != nil {
			failure(w, 400, e)
			return
		}
	}
	z, err := zip.NewReader(bytes.NewReader(bundle), int64(len(bundle)))
	if err != nil {
		failure(w, 400, err)
		return
	}
	entries := make(map[string]*zip.File)
	for _, f := range z.File {
		if _, ok := entries[f.Name]; ok {
			failure(w, 400, errors.New("duplicate ZIP entry"))
			return
		}
		entries[f.Name] = f
	}
	manifest, ok := entries["manifest.json"]
	if !ok || manifest.UncompressedSize64 > 1<<20 {
		failure(w, 400, errors.New("manifest required"))
		return
	}
	f, e := manifest.Open()
	if e != nil {
		failure(w, 400, e)
		return
	}
	b, e := io.ReadAll(io.LimitReader(f, 1<<20))
	_ = f.Close()
	if e != nil {
		failure(w, 400, e)
		return
	}
	var voices []message
	if json.Unmarshal(b, &voices) != nil || len(voices) == 0 || len(voices) > 100 {
		failure(w, 400, errors.New("invalid voice manifest"))
		return
	}
	files := []message{}
	total := 0
	seen := make(map[string]bool)
	for _, v := range voices {
		role, emotion := str(v, "role"), str(v, "emotion")
		path := role + "/" + role + "_" + emotion + ".pt"
		entry, ok := entries[path]
		if !safeName.MatchString(role) || !safeName.MatchString(emotion) || str(v, "path") != path || !ok || seen[path] || entry.UncompressedSize64 > maxBody {
			failure(w, 400, errors.New("invalid voice path or size"))
			return
		}
		seen[path] = true
		h, e := entry.Open()
		if e != nil {
			failure(w, 400, e)
			return
		}
		data, e := io.ReadAll(io.LimitReader(h, maxBody+1))
		_ = h.Close()
		total += len(data)
		if e != nil || len(data) == 0 || total > maxBody {
			failure(w, 400, errors.New("voice bundle exceeds limit"))
			return
		}
		digest := sha256.Sum256(data)
		if hex.EncodeToString(digest[:]) != str(v, "sha256") {
			failure(w, 400, errors.New("PT SHA256 mismatch"))
			return
		}
		files = append(files, message{"path": path, "sha256": str(v, "sha256"), "content_base64": base64.StdEncoding.EncodeToString(data)})
	}
	id := identifier()
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Minute)
	defer cancel()
	m, err := g.call(ctx, "sync:"+id, message{"type": "voice.sync", "sync_id": id, "files": files})
	if err != nil {
		failure(w, 503, err)
		return
	}
	if str(m, "status") != "ok" {
		failure(w, 502, errors.New(str(m, "error")))
		return
	}
	installed, _ := m["files"].([]any)
	received := make(map[string]string)
	for _, raw := range installed {
		if file, ok := raw.(map[string]any); ok {
			received[str(file, "path")] = str(file, "sha256")
		}
	}
	for _, v := range voices {
		if received[str(v, "path")] != str(v, "sha256") {
			failure(w, 502, errors.New("worker sync acknowledgement mismatch"))
			return
		}
	}
	reply(w, 200, message{"voices": voices})
}

func main() {
	client, token, id := os.Getenv("CLIENT_TOKEN"), os.Getenv("WORKER_TOKEN"), os.Getenv("WORKER_ID")
	if len(client) < 32 || len(token) < 32 || client == token || !safeName.MatchString(id) {
		log.Fatal("distinct tokens (minimum 32 chars) and WORKER_ID required")
	}
	addr := os.Getenv("LISTEN_ADDR")
	if addr == "" {
		addr = "127.0.0.1:18765"
	}
	g := newGateway(client, token, id)
	srv := &http.Server{Addr: addr, Handler: g.handler(), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 75 * time.Second, MaxHeaderBytes: 16 << 10}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go func() {
		<-ctx.Done()
		g.mu.Lock()
		if g.active != nil {
			_ = g.active.conn.Close()
		}
		g.mu.Unlock()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Printf("gateway listening address=%s worker=%s", addr, id)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(fmt.Errorf("server: %w", err))
	}
}
