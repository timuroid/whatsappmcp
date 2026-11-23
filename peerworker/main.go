// The worker exchanges batches privately with the local archive over stdio.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"rsc.io/qr"
)

const pageSize = 50
const maxFileSize = 512 * 1024 * 1024

type anchor struct {
	ID        string `json:"id"`
	Timestamp int64  `json:"timestamp"`
	FromMe    bool   `json:"from_me"`
}

type command struct {
	Account string `json:"account"`
	Chat    string `json:"chat"`
	Before  anchor `json:"before"`
}

type attachment struct {
	Filename string `json:"filename"`
	MIME     string `json:"mime"`
	Status   string `json:"status"`
	Path     string `json:"path,omitempty"`
	Size     int64  `json:"size,omitempty"`
}

type message struct {
	anchor
	Text        string       `json:"text"`
	Author      string       `json:"author"`
	ReplyTo     string       `json:"reply_to"`
	Kind        string       `json:"type"`
	Deleted     bool         `json:"deleted"`
	Attachments []attachment `json:"attachments"`
}

type result struct {
	Status   string    `json:"status"`
	Messages []message `json:"messages,omitempty"`
}

func normalize(value string) types.JID {
	parsed, _ := types.ParseJID(strings.ReplaceAll(value, "@c.us", "@s.whatsapp.net"))
	return parsed.ToNonAD()
}

func openDevice(ctx context.Context, location, account string) (*sqlstore.Container, *whatsmeow.Client, string) {
	path, err := filepath.Abs(location)
	if err != nil {
		return nil, nil, "session_unavailable"
	}
	dsn := url.URL{Scheme: "file", Path: path, RawQuery: "mode=rw&_foreign_keys=on&_busy_timeout=5000"}
	container, err := sqlstore.New(ctx, "sqlite3", dsn.String(), nil)
	if err != nil {
		return nil, nil, "session_unavailable"
	}
	devices, err := container.GetAllDevices(ctx)
	if err != nil || len(devices) != 1 || devices[0].GetJID().IsEmpty() {
		container.Close()
		return nil, nil, "session_unavailable"
	}
	device := devices[0]
	expected := normalize(account)
	if expected.IsEmpty() || (expected != device.GetJID().ToNonAD() && expected != device.GetLID().ToNonAD()) {
		container.Close()
		return nil, nil, "account_mismatch"
	}
	client := whatsmeow.NewClient(device, nil)
	client.EnableAutoReconnect = false
	return container, client, ""
}

func sameChat(ctx context.Context, client *whatsmeow.Client, a, b types.JID) bool {
	if a == b {
		return true
	}
	alt, err := client.Store.GetAltJID(ctx, a)
	return err == nil && !alt.IsEmpty() && alt.ToNonAD() == b
}

func fetch(client *whatsmeow.Client, updates <-chan *waHistorySync.HistorySync, c command, media string) result {
	chat := normalize(c.Chat)
	if chat.IsEmpty() || c.Before.ID == "" || c.Before.Timestamp <= 0 ||
		(chat.Server != types.GroupServer && chat.Server != types.DefaultUserServer && chat.Server != types.HiddenUserServer) {
		return result{Status: "invalid_anchor"}
	}
	// Discard buffered replies to an earlier attempt before issuing a new request.
	for len(updates) > 0 {
		<-updates
	}
	info := &types.MessageInfo{
		MessageSource: types.MessageSource{Chat: chat, IsFromMe: c.Before.FromMe},
		ID:            c.Before.ID, Timestamp: time.Unix(c.Before.Timestamp, 0),
	}
	ctx, cancel := context.WithTimeout(context.Background(), 18*time.Second)
	_, err := client.SendPeerMessage(ctx, client.BuildHistorySyncRequest(info, pageSize))
	cancel()
	if err != nil {
		return result{Status: "request_failed"}
	}

	timer := time.NewTimer(35 * time.Second)
	defer timer.Stop()
	page := result{Status: "timeout"}
	var accepted []*waHistorySync.HistorySyncMsg
	pending := true
	for pending {
		select {
		case <-timer.C:
			pending = false
		case data := <-updates:
			for _, conversation := range data.GetConversations() {
				if !sameChat(context.Background(), client, normalize(conversation.GetID()), chat) {
					continue
				}
				page.Status = "received"
				accepted = append(accepted, conversation.GetMessages()...)
				if conversation.EndOfHistoryTransferType != nil {
					switch conversation.GetEndOfHistoryTransferType() {
					case waHistorySync.Conversation_COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY:
						page.Status = "device_start"
					case waHistorySync.Conversation_COMPLETE_ON_DEMAND_SYNC_WITH_MORE_MSG_ON_PRIMARY_BUT_NO_ACCESS:
						page.Status = "device_limit"
					}
				}
				// Collect any immediately following chunks before advancing the cursor.
				timer.Reset(2 * time.Second)
			}
		}
	}
	seen := make(map[string]bool)
	for _, item := range accepted {
		raw := item.GetMessage()
		key := raw.GetKey()
		id := key.GetID()
		stamp := int64(raw.GetMessageTimestamp())
		if id == "" || seen[id] || id == c.Before.ID || stamp <= 0 || stamp > c.Before.Timestamp {
			continue
		}
		if remote := key.GetRemoteJID(); remote != "" &&
			!sameChat(context.Background(), client, normalize(remote), chat) {
			continue
		}
		seen[id] = true
		evt, err := client.ParseWebMessage(chat, raw)
		if err != nil || evt.Message == nil {
			// Keep positions even when a historic message has no readable payload.
			page.Messages = append(page.Messages, message{
				anchor: anchor{ID: id, Timestamp: stamp, FromMe: key.GetFromMe()},
				Kind:   "other", Attachments: []attachment{},
			})
			continue
		}
		page.Messages = append(page.Messages, parseMessage(client, evt, media))
	}
	return page
}

type fileMessage interface {
	whatsmeow.DownloadableMessage
	GetFileLength() uint64
	GetMimetype() string
}

func parseMessage(client *whatsmeow.Client, evt *events.Message, media string) message {
	info, msg := evt.Info, evt.Message
	row := message{
		anchor: anchor{ID: info.ID, Timestamp: info.Timestamp.Unix(), FromMe: info.IsFromMe},
		Author: info.PushName, Text: msg.GetConversation(), Kind: "text", Attachments: []attachment{},
	}
	if ext := msg.GetExtendedTextMessage(); ext != nil {
		row.Text, row.ReplyTo = ext.GetText(), ext.GetContextInfo().GetStanzaID()
	}
	var file fileMessage
	var filename string
	switch {
	case msg.GetImageMessage() != nil:
		item := msg.GetImageMessage()
		file, row.Kind, row.Text, row.ReplyTo = item, "image", item.GetCaption(), item.GetContextInfo().GetStanzaID()
	case msg.GetVideoMessage() != nil:
		item := msg.GetVideoMessage()
		file, row.Kind, row.Text, row.ReplyTo = item, "video", item.GetCaption(), item.GetContextInfo().GetStanzaID()
	case msg.GetPtvMessage() != nil:
		item := msg.GetPtvMessage()
		file, row.Kind, row.Text = item, "video", item.GetCaption()
	case msg.GetAudioMessage() != nil:
		item := msg.GetAudioMessage()
		file, row.Kind, row.ReplyTo = item, "audio", item.GetContextInfo().GetStanzaID()
	case msg.GetDocumentMessage() != nil:
		item := msg.GetDocumentMessage()
		file, row.Kind, filename, row.Text = item, "document", item.GetFileName(), item.GetCaption()
		row.ReplyTo = item.GetContextInfo().GetStanzaID()
	case msg.GetStickerMessage() != nil:
		file, row.Kind = msg.GetStickerMessage(), "sticker"
	case msg.GetProtocolMessage().GetType() == waE2E.ProtocolMessage_REVOKE && msg.GetProtocolMessage() != nil:
		row.Deleted = true
	}
	if file != nil {
		row.Attachments = append(row.Attachments, download(client, file, filename, media))
	} else if row.Text == "" && !row.Deleted {
		row.Kind = "other"
	}
	return row
}

func download(client *whatsmeow.Client, file fileMessage, filename, media string) attachment {
	item := attachment{Filename: filename, MIME: file.GetMimetype(), Status: "unavailable"}
	if file.GetFileLength() > maxFileSize {
		item.Status = "too_large"
		return item
	}
	if media == "" {
		item.Status = "skipped"
		return item
	}
	temp, err := os.CreateTemp(media, "attachment-")
	if err != nil {
		item.Status = "storage_error"
		return item
	}
	defer temp.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	if err := client.DownloadToFile(ctx, file, temp); err != nil {
		os.Remove(temp.Name())
		return item
	}
	stat, err := temp.Stat()
	if err != nil || stat.Size() > maxFileSize {
		os.Remove(temp.Name())
		return item
	}
	item.Status, item.Path, item.Size = "downloaded", filepath.Base(temp.Name()), stat.Size()
	return item
}

func run(location, media string, decoder *json.Decoder, encoder *json.Encoder) error {
	var c command
	if err := decoder.Decode(&c); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	container, client, reason := openDevice(ctx, location, c.Account)
	if reason != "" {
		return encoder.Encode(result{Status: reason})
	}
	defer container.Close()
	updates := make(chan *waHistorySync.HistorySync, 16)
	client.AddEventHandler(func(event any) {
		if synced, ok := event.(*events.HistorySync); ok && synced.Data.GetSyncType() == waHistorySync.HistorySync_ON_DEMAND {
			select {
			case updates <- synced.Data:
			default:
			}
		}
	})
	if err := client.ConnectContext(ctx); err != nil {
		return encoder.Encode(result{Status: "session_unavailable"})
	}
	defer client.Disconnect()
	if !client.WaitForConnection(15*time.Second) || !client.IsLoggedIn() {
		return encoder.Encode(result{Status: "session_unavailable"})
	}
	owner := c.Account
	for {
		if c.Account != owner {
			return encoder.Encode(result{Status: "account_mismatch"})
		}
		if err := encoder.Encode(fetch(client, updates, c, media)); err != nil {
			return err
		}
		if err := decoder.Decode(&c); err != nil {
			return err
		}
	}
}

func main() {
	store := flag.String("store", "", "absolute device database path")
	media := flag.String("media", "", "private export directory")
	pairing := flag.Bool("pair", false, "register a separate linked device")
	flag.Parse()
	decoder, encoder := json.NewDecoder(os.Stdin), json.NewEncoder(os.Stdout)
	action := run
	if *pairing {
		action = pair
	}
	if err := action(*store, *media, decoder, encoder); err != nil && !errors.Is(err, io.EOF) {
		fmt.Fprintln(os.Stderr, "whatsapp history worker stopped")
		os.Exit(1)
	}
}

func pair(location, _ string, decoder *json.Decoder, encoder *json.Encoder) error {
	var c command
	if err := decoder.Decode(&c); err != nil {
		return err
	}
	expected := normalize(c.Account)
	if location == "" || expected.IsEmpty() {
		return encoder.Encode(result{Status: "account_mismatch"})
	}
	path, err := filepath.Abs(location)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	dsn := url.URL{Scheme: "file", Path: path, RawQuery: "mode=rwc&_foreign_keys=on&_busy_timeout=5000"}
	container, err := sqlstore.New(ctx, "sqlite3", dsn.String(), nil)
	if err != nil {
		return err
	}
	defer container.Close()
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return err
	}
	if !device.GetJID().IsEmpty() {
		if expected != device.GetJID().ToNonAD() && expected != device.GetLID().ToNonAD() {
			return encoder.Encode(result{Status: "account_mismatch"})
		}
		return encoder.Encode(result{Status: "paired"})
	}
	client := whatsmeow.NewClient(device, nil)
	channel, err := client.GetQRChannel(ctx)
	if err != nil {
		return err
	}
	if err = client.ConnectContext(ctx); err != nil {
		return err
	}
	defer client.Disconnect()
	for event := range channel {
		switch event.Event {
		case whatsmeow.QRChannelEventCode:
			code, err := qr.Encode(event.Code, qr.M)
			if err != nil {
				return err
			}
			if err := encoder.Encode(struct {
				Status string `json:"status"`
				PNG    []byte `json:"png"`
			}{"qr", code.PNG()}); err != nil {
				return err
			}
		case "success":
			if expected != device.GetJID().ToNonAD() && expected != device.GetLID().ToNonAD() {
				return encoder.Encode(result{Status: "account_mismatch"})
			}
			return encoder.Encode(result{Status: "paired"})
		default:
			return encoder.Encode(result{Status: "pairing_stopped"})
		}
	}
	return encoder.Encode(result{Status: "pairing_stopped"})
}
