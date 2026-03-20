// trade_executor.go — Ultra-low latency order router for BNF Engine v14.
//
// This compiled Go binary listens on a local TCP socket (port 9559)
// for JSON trade signals from the Python brain (main.py / execution_agent.py).
//
// When a signal arrives, it instantly forwards the order to Zerodha Kite
// via their REST API, bypassing Python's GIL entirely.
//
// Architecture:
//   Python (Brain) --TCP JSON--> Go (Trigger) --HTTPS--> Zerodha Kite API

package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config
const (
	LISTEN_ADDR = "127.0.0.1:9559"
	KITE_BASE   = "https://api.kite.trade"
)

// TradeSignal is the JSON payload received from Python
type TradeSignal struct {
	Action       string  `json:"action"`        // "BUY" or "SELL"
	Symbol       string  `json:"symbol"`        // "RELIANCE"
	Exchange     string  `json:"exchange"`       // "NSE"
	Qty          int     `json:"qty"`            // 100
	Price        float64 `json:"price"`          // 0 = MARKET
	TriggerPrice float64 `json:"trigger_price"`  // for SL orders
	OrderType    string  `json:"order_type"`     // "MARKET", "LIMIT", "SL"
	Product      string  `json:"product"`        // "MIS" or "CNC"
	Validity     string  `json:"validity"`       // "DAY"
	Tag          string  `json:"tag"`            // "S5_VWAP" etc.
}

// TradeResponse sent back to Python
type TradeResponse struct {
	Status  string `json:"status"`   // "OK" or "ERROR"
	OrderID string `json:"order_id"` // Kite order ID
	Message string `json:"message"`  // Error detail or confirmation
	LatencyUs int64 `json:"latency_us"` // Execution latency in microseconds
}

var (
	apiKey      string
	accessToken string
	httpClient  *http.Client
)

func init() {
	apiKey = os.Getenv("KITE_API_KEY")
	accessToken = os.Getenv("KITE_ACCESS_TOKEN")

	// Optimized HTTP client — persistent connections, no idle timeout waste
	httpClient = &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			MaxIdleConns:        10,
			MaxIdleConnsPerHost: 10,
			IdleConnTimeout:     90 * time.Second,
			DisableKeepAlives:   false,
		},
	}
}

func placeOrder(sig TradeSignal) TradeResponse {
	start := time.Now()

	if apiKey == "" || accessToken == "" {
		return TradeResponse{
			Status:  "ERROR",
			Message: "KITE_API_KEY or KITE_ACCESS_TOKEN not set",
		}
	}

	// Build form data for Kite order API
	data := url.Values{}
	data.Set("tradingsymbol", sig.Symbol)
	data.Set("exchange", sig.Exchange)
	data.Set("transaction_type", sig.Action)
	data.Set("quantity", strconv.Itoa(sig.Qty))
	data.Set("product", sig.Product)
	data.Set("order_type", sig.OrderType)
	data.Set("validity", sig.Validity)

	if sig.OrderType == "LIMIT" {
		data.Set("price", fmt.Sprintf("%.2f", sig.Price))
	}
	if sig.OrderType == "SL" || sig.OrderType == "SL-M" {
		data.Set("trigger_price", fmt.Sprintf("%.2f", sig.TriggerPrice))
		if sig.OrderType == "SL" {
			data.Set("price", fmt.Sprintf("%.2f", sig.Price))
		}
	}
	if sig.Tag != "" {
		data.Set("tag", sig.Tag)
	}

	// Fire the order
	reqURL := KITE_BASE + "/orders/regular"
	req, err := http.NewRequest("POST", reqURL, strings.NewReader(data.Encode()))
	if err != nil {
		return TradeResponse{Status: "ERROR", Message: err.Error()}
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("X-Kite-Version", "3")
	req.Header.Set("Authorization", "token "+apiKey+":"+accessToken)

	resp, err := httpClient.Do(req)
	if err != nil {
		return TradeResponse{Status: "ERROR", Message: err.Error()}
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	elapsed := time.Since(start).Microseconds()

	// Parse Kite response
	var kiteResp struct {
		Status string `json:"status"`
		Data   struct {
			OrderID string `json:"order_id"`
		} `json:"data"`
		Message string `json:"message"`
	}
	json.Unmarshal(body, &kiteResp)

	if kiteResp.Status == "success" {
		return TradeResponse{
			Status:    "OK",
			OrderID:   kiteResp.Data.OrderID,
			Message:   fmt.Sprintf("Order placed in %dμs", elapsed),
			LatencyUs: elapsed,
		}
	}

	return TradeResponse{
		Status:    "ERROR",
		Message:   kiteResp.Message,
		LatencyUs: elapsed,
	}
}

func handleConnection(conn net.Conn) {
	defer conn.Close()
	decoder := json.NewDecoder(conn)

	for {
		var sig TradeSignal
		err := decoder.Decode(&sig)
		if err != nil {
			if err != io.EOF {
				log.Printf("[GoExec] Decode error: %v", err)
			}
			return
		}

		log.Printf("[GoExec] Received: %s %s x%d (%s)", sig.Action, sig.Symbol, sig.Qty, sig.OrderType)

		// Execute instantly
		result := placeOrder(sig)

		// Send response back to Python
		respBytes, _ := json.Marshal(result)
		respBytes = append(respBytes, '\n')
		conn.Write(respBytes)

		log.Printf("[GoExec] Result: %s | %s | %dμs", result.Status, result.Message, result.LatencyUs)
	}
}

func main() {
	fmt.Println("===========================================")
	fmt.Println("  BNF ENGINE v14 — Go Trade Executor")
	fmt.Println("  Listening on", LISTEN_ADDR)
	fmt.Println("===========================================")

	if apiKey == "" {
		log.Println("[GoExec] WARNING: KITE_API_KEY not set. Orders will fail.")
	}

	ln, err := net.Listen("tcp", LISTEN_ADDR)
	if err != nil {
		log.Fatalf("[GoExec] Failed to listen: %v", err)
	}
	defer ln.Close()

	// Pre-warm HTTP connection to Kite
	warmReq, _ := http.NewRequest("GET", KITE_BASE+"/user/margins", nil)
	if warmReq != nil && apiKey != "" {
		warmReq.Header.Set("Authorization", "token "+apiKey+":"+accessToken)
		warmReq.Header.Set("X-Kite-Version", "3")
		resp, err := httpClient.Do(warmReq)
		if err == nil {
			io.ReadAll(resp.Body)
			resp.Body.Close()
			log.Println("[GoExec] HTTP connection to Kite pre-warmed")
		}
	}

	_ = bytes.NewReader // keep import

	log.Println("[GoExec] Ready — waiting for Python signals...")
	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("[GoExec] Accept error: %v", err)
			continue
		}
		go handleConnection(conn)
	}
}
