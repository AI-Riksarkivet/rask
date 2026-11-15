// bus-signer is the DEVELOPMENT stand-in for the platform's NATS auth-callout signer.
//
// A pod presents only its projected ServiceAccount token as the CONNECT auth_token. This process receives the server's
// authorization request on $SYS.REQ.USER.AUTH, checks the token with a Kubernetes TokenReview for the bus audience, picks
// the bus user the pod named (connection name) among the users the permission table binds to that ServiceAccount, and
// answers with a short-lived user signed by that bus user's scoped account signing key. The user carries no permissions
// of its own: the scope lives on the key and the server enforces it. A platform runs its own signer against the same
// permission table; nothing in rask depends on this one.
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
)

const (
	authSubject = "$SYS.REQ.USER.AUTH"
	queueGroup  = "bus-signer"
	saDir       = "/var/run/secrets/kubernetes.io/serviceaccount"
)

// table is permissions.json: only the fields the signer needs (the scope is on each user's key).
type table struct {
	Audience       string `json:"audience"`
	SessionSeconds int    `json:"sessionSeconds"`
	Users          map[string]struct {
		ServiceAccount string `json:"serviceAccount"`
	} `json:"users"`
}

type signer struct {
	issuer                     nkeys.KeyPair
	authAccount, appAccount    string
	keysDir, tablePath         string
	reviewURL, reviewTokenPath string
	reviewClient               *http.Client
	mu                         sync.Mutex
	tbl                        table
	tblMod                     time.Time
}

type refusal struct{ reason, serviceAccount string }

func (r refusal) Error() string { return r.reason }

func main() {
	url := flag.String("url", "", "NATS server URL")
	creds := flag.String("creds", "", "the callout user's creds file")
	ca := flag.String("ca", "", "CA bundle for the bus's TLS (optional)")
	issuerSeed := flag.String("issuer-seed", "", "file holding the AUTH account signing seed")
	authAccountFile := flag.String("auth-account-file", "", "file holding the AUTH account public key")
	appAccountFile := flag.String("app-account-file", "", "file holding the APP account public key")
	keys := flag.String("keys", "", "directory with one scoped-key seed file per session user, named after the user")
	tablePath := flag.String("table", "", "permissions.json, re-read when its mtime changes")
	reviewURL := flag.String("token-review-url", "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews", "TokenReview endpoint")
	reviewCA := flag.String("token-review-ca", saDir+"/ca.crt", "CA bundle of the TokenReview endpoint")
	reviewToken := flag.String("token-review-token", saDir+"/token", "bearer for the TokenReview call, re-read on every call")
	flag.Parse()
	for name, v := range map[string]string{"url": *url, "creds": *creds, "issuer-seed": *issuerSeed, "auth-account-file": *authAccountFile, "app-account-file": *appAccountFile, "keys": *keys, "table": *tablePath} {
		if v == "" {
			log.Fatalf("--%s is required", name)
		}
	}

	seed, err := os.ReadFile(*issuerSeed)
	must(err)
	issuer, err := nkeys.FromSeed(bytes.TrimSpace(seed))
	must(err)
	authAccount, err := os.ReadFile(*authAccountFile)
	must(err)
	appAccount, err := os.ReadFile(*appAccountFile)
	must(err)
	pool, err := loadPool(*reviewCA)
	must(err)
	s := &signer{
		issuer: issuer, authAccount: string(bytes.TrimSpace(authAccount)), appAccount: string(bytes.TrimSpace(appAccount)), keysDir: *keys, tablePath: *tablePath,
		reviewURL: *reviewURL, reviewTokenPath: *reviewToken,
		reviewClient: &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}}},
	}
	_, err = s.loadTable()
	must(err)

	opts := []nats.Option{nats.UserCredentials(*creds), nats.Name("bus-signer"), nats.MaxReconnects(-1)}
	if *ca != "" {
		// The handshake comes before the server's greeting: a client that reads the greeting first takes the server's
		// word, in plaintext, for whether the connection is to be encrypted at all.
		opts = append(opts, nats.RootCAs(*ca), nats.TLSHandshakeFirst())
	}
	nc, err := nats.Connect(*url, opts...)
	must(err)
	sub, err := nc.QueueSubscribe(authSubject, queueGroup, func(m *nats.Msg) { go s.handle(m) })
	must(err)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	<-stop
	_ = sub.Drain()
	_ = nc.Drain()
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}

func loadPool(path string) (*x509.CertPool, error) {
	pem, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("%s holds no certificate", path)
	}
	return pool, nil
}

func (s *signer) loadTable() (table, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, err := os.Stat(s.tablePath)
	if err != nil {
		return table{}, err
	}
	if !st.ModTime().Equal(s.tblMod) {
		raw, err := os.ReadFile(s.tablePath)
		if err != nil {
			return table{}, err
		}
		var t table
		if err := json.Unmarshal(raw, &t); err != nil {
			return table{}, err
		}
		if t.Audience == "" || t.SessionSeconds <= 0 {
			return table{}, errors.New("permission table lacks audience or sessionSeconds")
		}
		s.tbl, s.tblMod = t, st.ModTime()
	}
	return s.tbl, nil
}

func (s *signer) handle(m *nats.Msg) {
	req, err := jwt.DecodeAuthorizationRequestClaims(string(m.Data))
	if err != nil {
		log.Printf("undecodable authorization request: %v", err)
		return
	}
	user, sa, err := s.decide(req)
	resp := jwt.NewAuthorizationResponseClaims(req.UserNkey)
	resp.Audience = req.Server.ID
	resp.IssuerAccount = s.authAccount
	event := map[string]string{"event": "bus_session_granted", "user": user, "service_account": sa}
	if err != nil {
		resp.Error = "authorization refused"
		event["event"], event["user"], event["reason"] = "bus_session_refused", req.ConnectOptions.Name, err.Error()
		var r refusal
		if errors.As(err, &r) {
			event["service_account"] = r.serviceAccount
		}
	} else {
		resp.Jwt, err = s.issue(req.UserNkey, user)
		if err != nil {
			resp.Error = "authorization refused"
			event["event"], event["reason"] = "bus_session_refused", err.Error()
		}
	}
	out, err := resp.Encode(s.issuer)
	if err != nil {
		log.Printf("encode response: %v", err)
		return
	}
	line, _ := json.Marshal(event)
	fmt.Println(string(line))
	_ = m.Respond([]byte(out))
}

// decide returns the bus user and the ServiceAccount the request is entitled to.
func (s *signer) decide(req *jwt.AuthorizationRequestClaims) (string, string, error) {
	tbl, err := s.loadTable()
	if err != nil {
		return "", "", fmt.Errorf("table unreadable: %w", err)
	}
	token, name := req.ConnectOptions.Token, req.ConnectOptions.Name
	if token == "" {
		return "", "", refusal{reason: "no token presented"}
	}
	sa, err := s.review(token, tbl.Audience)
	if err != nil {
		return "", "", refusal{reason: "token review: " + err.Error()}
	}
	row, ok := tbl.Users[name]
	if !ok || row.ServiceAccount != sa {
		return "", "", refusal{reason: "service account is not that bus user's", serviceAccount: sa}
	}
	return name, sa, nil
}

// issue signs a user for subject with the bus user's scoped key; the user sets no permissions.
func (s *signer) issue(subject, user string) (string, error) {
	tbl, _ := s.loadTable()
	seed, err := os.ReadFile(filepath.Join(s.keysDir, user))
	if err != nil {
		return "", fmt.Errorf("scoped key unreadable: %w", err)
	}
	key, err := nkeys.FromSeed(bytes.TrimSpace(seed))
	if err != nil {
		return "", fmt.Errorf("scoped key invalid: %w", err)
	}
	uc := jwt.NewUserClaims(subject)
	uc.Name = user
	// NewUserClaims fills in unlimited limits; a scoped key refuses a user that sets any, so they are cleared.
	uc.UserPermissionLimits = jwt.UserPermissionLimits{}
	uc.Audience = s.appAccount
	uc.IssuerAccount = s.appAccount
	uc.Expires = time.Now().Add(time.Duration(tbl.SessionSeconds) * time.Second).Unix()
	return uc.Encode(key)
}

// review asks the API server whether the token is valid for the audience and returns its username.
func (s *signer) review(token, audience string) (string, error) {
	bearer, err := os.ReadFile(s.reviewTokenPath)
	if err != nil {
		return "", errors.New("reviewer token unreadable")
	}
	body, _ := json.Marshal(map[string]any{
		"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
		"spec": map[string]any{"token": token, "audiences": []string{audience}},
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	hr, err := http.NewRequestWithContext(ctx, http.MethodPost, s.reviewURL, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	hr.Header.Set("Content-Type", "application/json")
	hr.Header.Set("Authorization", "Bearer "+string(bytes.TrimSpace(bearer)))
	res, err := s.reviewClient.Do(hr)
	if err != nil {
		return "", errors.New("review call failed")
	}
	defer res.Body.Close()
	var out struct {
		Status struct {
			Authenticated bool     `json:"authenticated"`
			Audiences     []string `json:"audiences"`
			User          struct {
				Username string `json:"username"`
			} `json:"user"`
		} `json:"status"`
	}
	if res.StatusCode/100 != 2 || json.NewDecoder(res.Body).Decode(&out) != nil {
		return "", fmt.Errorf("review answered %d", res.StatusCode)
	}
	st := out.Status
	if !st.Authenticated || st.User.Username == "" {
		return "", errors.New("not authenticated")
	}
	for _, a := range st.Audiences {
		if a == audience {
			return st.User.Username, nil
		}
	}
	return "", errors.New("audience not granted")
}
