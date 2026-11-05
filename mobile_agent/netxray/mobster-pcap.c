/*
 * mobster-pcap: record an iPhone's network metadata with Apple's com.apple.pcapd service (Network X-ray).
 *
 *   mobster-pcap --udid UDID [--network] [--seconds N] [--max-mb N] [--snaplen N] --out FILE.pcapng
 *   mobster-pcap --from-stream FILE [--snaplen N] --out FILE.pcapng
 *
 * pcapd needs the phone to trust this Mac and nothing else: no Developer Mode, no Xcode, no VPN. It streams every
 * packet the phone sends or receives with the process that sent it. This tool writes them as pcapng, with the process
 * in a comment on each packet ("PID: 412, ProcName: Weather, EPID: 412, EProcName: Weather, SVC: 0"), the direction
 * in the packet's flags, and one interface per link layer and interface name (Ethernet 1, raw IP 101, BSD loopback 0).
 *
 * It writes only the file --out names, which it creates and never overwrites (mode 0600). Each packet keeps its
 * first --snaplen bytes (default 256): the headers, not the payload. DNS, a TLS ClientHello and a QUIC Initial keep
 * more, because the hostname is in them: a hostname, not a page. It decrypts nothing.
 *
 * --network uses only the Wi-Fi connection, even with a cable in, and exits 5 when the service isn't encrypted.
 *
 * It stops, flushes and exits 0 on SIGINT, SIGTERM, --seconds or --max-mb.
 * --from-stream reads a file of length-prefixed pcapd messages (a 4-byte big-endian length, then that many bytes of
 * the message's data) instead of a phone, with the times the messages carry. It is for tests.
 *
 * Exit codes: 0 done, 2 usage, 3 no iPhone with that UDID, 4 the iPhone doesn't trust this Mac (or is locked),
 * 5 the iPhone refused the pcapd service, 6 the file couldn't be written, 7 the stream file couldn't be read,
 * 8 the connection to the iPhone broke. One line on stderr says what happened.
 *
 * The message layout (all integers big-endian unless noted) is what pcapd sends, published in several tools:
 *    0 u32 header length    4 u8 version        5 u32 frame length   9 u8 interface type  10 u16 unit
 *   12 u8 io (0 in, 1 out) 13 u32 family       17 u32 link pre-length 21 u32 post-length
 *   25 char[16] interface  41 u32 pid (little-endian)   45 char[17] process   62 u32 service class
 *   66 u32 effective pid (little-endian)  70 char[17] effective process   87 u32 seconds  91 u32 microseconds
 * and the frame follows at the header length. This file is MIT, like Mobster. desktop/scripts/build-iphone-tools.sh
 * compiles it against the static libimobiledevice and libplist it bundles (LGPL-2.1, like the iPhone tools).
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <libimobiledevice/libimobiledevice.h>
#include <libimobiledevice/lockdown.h>
#include <libimobiledevice/property_list_service.h>
#include <plist/plist.h>

enum { DONE = 0, USAGE = 2, NO_DEVICE = 3, NOT_TRUSTED = 4, REFUSED = 5, WRITE_FAILED = 6, BAD_STREAM = 7, BROKEN = 8 };

#define SERVICE "com.apple.pcapd"
#define HEADER_MIN 95
#define MAX_MESSAGE (1u << 20)
#define MAX_INTERFACES 32
#define DEFAULT_SNAPLEN 256
#define DNS_SNAPLEN 1500
#define HELLO_SNAPLEN 1024
#define INITIAL_SNAPLEN 1500

static volatile sig_atomic_t stop_requested = 0;

static void on_signal(int signal_number)
{
	(void)signal_number;
	stop_requested = 1;
}

static int usage(void)
{
	fprintf(stderr, "usage: mobster-pcap --udid UDID [--network] [--seconds N] [--max-mb N] [--snaplen N] --out FILE.pcapng\n"
	                "       mobster-pcap --from-stream FILE [--snaplen N] --out FILE.pcapng\n");
	return USAGE;
}

static int valid_udid(const char *udid)
{
	size_t length = strlen(udid);
	if (length < 24 || length > 40)
		return 0;
	for (size_t i = 0; i < length; i++) {
		char c = udid[i];
		if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F') || c == '-'))
			return 0;
	}
	return 1;
}

static int handshake_code(lockdownd_error_t error)
{
	switch (error) {
	case LOCKDOWN_E_PASSWORD_PROTECTED:
	case LOCKDOWN_E_USER_DENIED_PAIRING:
	case LOCKDOWN_E_PAIRING_DIALOG_RESPONSE_PENDING:
	case LOCKDOWN_E_INVALID_HOST_ID:
	case LOCKDOWN_E_MISSING_PAIR_RECORD:
	case LOCKDOWN_E_INVALID_PAIR_RECORD:
	case LOCKDOWN_E_PAIRING_FAILED:
	case LOCKDOWN_E_SSL_ERROR:
		return NOT_TRUSTED;
	default:
		return BROKEN;
	}
}

/* ---- big-endian and little-endian reads ---- */

static uint32_t be32(const uint8_t *p) { return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3]; }
static uint32_t le32(const uint8_t *p) { return (uint32_t)p[3] << 24 | (uint32_t)p[2] << 16 | (uint32_t)p[1] << 8 | p[0]; }
static uint16_t be16(const uint8_t *p) { return (uint16_t)(p[0] << 8 | p[1]); }

/* ---- the output file ---- */

typedef struct {
	char name[17];
	uint32_t linktype;
} interface_t;

typedef struct {
	FILE *file;
	uint64_t written;
	uint64_t limit; /* 0: none */
	int failed;
	interface_t interfaces[MAX_INTERFACES];
	int interface_count;
	uint32_t snaplen;
} output_t;

static void put(output_t *out, const void *data, size_t length)
{
	if (!out->failed && fwrite(data, 1, length, out->file) != length)
		out->failed = 1;
	out->written += length;
}

static void put32(output_t *out, uint32_t value)
{
	uint8_t b[4] = {value & 0xff, value >> 8 & 0xff, value >> 16 & 0xff, value >> 24 & 0xff};
	put(out, b, 4);
}

static void put16(output_t *out, uint16_t value)
{
	uint8_t b[2] = {value & 0xff, value >> 8 & 0xff};
	put(out, b, 2);
}

static void put_option(output_t *out, uint16_t code, const void *data, size_t length)
{
	static const uint8_t zeros[4] = {0, 0, 0, 0};
	put16(out, code);
	put16(out, (uint16_t)length);
	put(out, data, length);
	put(out, zeros, (4 - length % 4) % 4);
}

static void write_section_header(output_t *out)
{
	static const char app[] = "mobster-pcap";
	uint32_t total = 12 + 16 + (4 + 12) + 4;
	put32(out, 0x0A0D0D0A);
	put32(out, total);
	put32(out, 0x1A2B3C4D);
	put16(out, 1);
	put16(out, 0);
	put32(out, 0xFFFFFFFF); /* section length: unknown (-1) */
	put32(out, 0xFFFFFFFF);
	put_option(out, 4, app, sizeof app - 1);
	put_option(out, 0, NULL, 0);
	put32(out, total);
}

static int interface_index(output_t *out, const char *name, uint32_t linktype)
{
	for (int i = 0; i < out->interface_count; i++)
		if (out->interfaces[i].linktype == linktype && !strcmp(out->interfaces[i].name, name))
			return i;
	if (out->interface_count >= MAX_INTERFACES)
		return -1;
	interface_t *entry = &out->interfaces[out->interface_count];
	snprintf(entry->name, sizeof entry->name, "%s", name);
	entry->linktype = linktype;
	size_t name_length = strlen(entry->name);
	size_t name_padded = (name_length + 3) & ~(size_t)3;
	uint32_t total = 12 + 8 + (name_length ? 4 + (uint32_t)name_padded : 0) + 8 + 4;
	uint8_t resolution = 6; /* microseconds */
	put32(out, 1);
	put32(out, total);
	put16(out, (uint16_t)linktype);
	put16(out, 0);
	put32(out, 262144); /* snaplen field: some packets keep more than the base snaplen */
	if (name_length)
		put_option(out, 2, entry->name, name_length);
	put_option(out, 9, &resolution, 1);
	put_option(out, 0, NULL, 0);
	put32(out, total);
	return out->interface_count++;
}

/* ---- what to keep of a frame ---- */

/* How many bytes of this frame to write. The base is snaplen. A DNS message, a TLS ClientHello and a QUIC Initial keep
 * more: the hostname is inside them, and it is metadata. Everything else keeps its headers and the first bytes after. */
static uint32_t keep_length(const uint8_t *ip, uint32_t length, uint32_t snaplen)
{
	if (length < 20)
		return snaplen;
	uint32_t header = 0;
	uint8_t protocol = 0;
	if ((ip[0] >> 4) == 4) {
		header = (uint32_t)(ip[0] & 15) * 4;
		protocol = ip[9];
		if (header < 20)
			return snaplen;
	} else if ((ip[0] >> 4) == 6 && length >= 40) {
		header = 40;
		protocol = ip[6];
		/* Extension headers are rare for TCP and UDP from a phone: a packet that has them keeps snaplen. */
	} else {
		return snaplen;
	}
	uint32_t keep = snaplen;
	if (protocol == 17 && length >= header + 8) { /* UDP */
		uint16_t sport = be16(ip + header), dport = be16(ip + header + 2);
		const uint8_t *payload = ip + header + 8;
		uint32_t available = length - header - 8;
		if (sport == 53 || dport == 53)
			keep = DNS_SNAPLEN;
		else if (dport == 443 && available > 0 && (payload[0] & 0xF0) == 0xC0)
			keep = INITIAL_SNAPLEN; /* a QUIC v1 Initial from the phone */
	} else if (protocol == 6 && length >= header + 20) { /* TCP */
		uint16_t sport = be16(ip + header), dport = be16(ip + header + 2);
		uint32_t offset = (uint32_t)(ip[header + 12] >> 4) * 4;
		if (offset >= 20 && length > header + offset + 2) {
			const uint8_t *payload = ip + header + offset;
			if (sport == 53 || dport == 53)
				keep = DNS_SNAPLEN;
			else if (payload[0] == 0x16 && payload[1] == 0x03 && payload[2] <= 4)
				keep = HELLO_SNAPLEN; /* a TLS handshake record */
		}
	}
	return keep > snaplen ? keep : snaplen;
}

/* ---- one pcapd message ---- */

static void clean_name(char *to, const uint8_t *from, size_t size)
{
	size_t length = 0;
	while (length < size && from[length])
		length++;
	size_t out = 0;
	for (size_t i = 0; i < length && out < 16 * 3; i++) {
		unsigned char c = from[i];
		to[out++] = c < 0x20 || c == 0x7f ? '?' : (char)c; /* names come from the phone: data, nothing else */
	}
	to[out] = 0;
}

/* Returns 1 when a packet was written, 0 when the message was skipped. */
static int handle(output_t *out, const uint8_t *m, size_t size, double arrival, int use_header_time)
{
	if (size < HEADER_MIN)
		return 0;
	uint32_t header = be32(m), frame_length = be32(m + 5), pre = be32(m + 17);
	if (header < HEADER_MIN || header > size || frame_length > size - header || frame_length == 0)
		return 0;
	const uint8_t *frame = m + header;
	char interface[17], process[64], effective[64];
	memcpy(interface, m + 25, 16);
	interface[16] = 0;
	clean_name(process, m + 45, 17);
	clean_name(effective, m + 70, 17);
	/* Interface names are data too: letters, digits and punctuation only. */
	for (char *c = interface; *c; c++)
		if ((unsigned char)*c < 0x21 || (unsigned char)*c > 0x7e)
			*c = '?';

	uint32_t linktype, skip = 0;
	if (pre == 14) {
		linktype = 1; /* Ethernet */
	} else if (pre == 0) {
		linktype = 101; /* raw IP */
	} else if (pre == 4) {
		linktype = 0; /* BSD loopback: a 4-byte family, then IP (the cellular interface) */
	} else if (pre < frame_length) {
		linktype = 101; /* an unknown link header: drop it, keep the IP packet */
		skip = pre;
	} else {
		return 0;
	}
	const uint8_t *data = frame + skip;
	uint32_t length = frame_length - skip;
	if (length == 0)
		return 0;

	uint32_t ip_offset = linktype == 1 ? 14 : linktype == 0 ? 4 : 0;
	uint32_t keep = out->snaplen;
	if (length > ip_offset)
		keep = keep_length(data + ip_offset, length - ip_offset, out->snaplen > ip_offset ? out->snaplen - ip_offset : 0)
		       + ip_offset;
	uint32_t captured = length < keep ? length : keep;

	int index = interface_index(out, interface, linktype);
	if (index < 0)
		return 0;

	double seconds = use_header_time ? (double)be32(m + 87) + (double)be32(m + 91) / 1e6 : arrival;
	uint64_t stamp = (uint64_t)(seconds * 1e6 + 0.5);

	char comment[256];
	int comment_length = snprintf(comment, sizeof comment, "PID: %u, ProcName: %s, EPID: %u, EProcName: %s, SVC: %u",
	                              le32(m + 41), process, le32(m + 66), effective, be32(m + 62));
	if (comment_length < 0)
		return 0;
	if ((size_t)comment_length >= sizeof comment)
		comment_length = sizeof comment - 1;
	size_t comment_padded = ((size_t)comment_length + 3) & ~(size_t)3;
	size_t data_padded = ((size_t)captured + 3) & ~(size_t)3;
	uint32_t total = (uint32_t)(32 + data_padded + (4 + comment_padded) + (4 + 4) + 4);
	if (out->limit && out->written + total > out->limit)
		return -1;

	uint32_t flags = m[12] ? 2 : 1; /* 1 inbound, 2 outbound */
	static const uint8_t zeros[4] = {0, 0, 0, 0};
	put32(out, 6);
	put32(out, total);
	put32(out, (uint32_t)index);
	put32(out, (uint32_t)(stamp >> 32));
	put32(out, (uint32_t)(stamp & 0xFFFFFFFF));
	put32(out, captured);
	put32(out, length);
	put(out, data, captured);
	put(out, zeros, data_padded - captured);
	put_option(out, 1, comment, (size_t)comment_length);
	uint8_t flag_bytes[4] = {flags & 0xff, 0, 0, 0};
	put_option(out, 2, flag_bytes, 4);
	put_option(out, 0, NULL, 0);
	put32(out, total);
	return 1;
}

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_REALTIME, &t);
	return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static double monotonic(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static int finish(output_t *out, int code)
{
	if (out->file) {
		if (fflush(out->file) != 0)
			out->failed = 1;
		if (fclose(out->file) != 0)
			out->failed = 1;
		out->file = NULL;
	}
	if (out->failed && code == DONE) {
		fprintf(stderr, "couldn't write the capture file\n");
		return WRITE_FAILED;
	}
	return code;
}

int main(int argc, char **argv)
{
	const char *udid = NULL, *stream_path = NULL, *out_path = NULL;
	long seconds = 0, max_mb = 0, snaplen = DEFAULT_SNAPLEN;
	int network_only = 0;
	for (int i = 1; i < argc; i++) {
		char *end = NULL;
		if (!strcmp(argv[i], "--udid") && i + 1 < argc)
			udid = argv[++i];
		else if (!strcmp(argv[i], "--from-stream") && i + 1 < argc)
			stream_path = argv[++i];
		else if (!strcmp(argv[i], "--out") && i + 1 < argc)
			out_path = argv[++i];
		else if (!strcmp(argv[i], "--seconds") && i + 1 < argc)
			seconds = strtol(argv[++i], &end, 10);
		else if (!strcmp(argv[i], "--max-mb") && i + 1 < argc)
			max_mb = strtol(argv[++i], &end, 10);
		else if (!strcmp(argv[i], "--snaplen") && i + 1 < argc)
			snaplen = strtol(argv[++i], &end, 10);
		else if (!strcmp(argv[i], "--network"))
			network_only = 1;
		else
			return usage();
		if (end && *end)
			return usage();
	}
	if (!out_path || (!udid == !stream_path) || seconds < 0 || max_mb < 0 || snaplen < 64 || snaplen > 65535)
		return usage();
	if (udid && !valid_udid(udid))
		return usage();

	output_t out;
	memset(&out, 0, sizeof out);
	out.snaplen = (uint32_t)snaplen;
	out.limit = (uint64_t)max_mb * 1024 * 1024;
	/* Create the file exclusively and private: a capture is never overwritten and nobody else reads it. */
	int fd = open(out_path, O_WRONLY | O_CREAT | O_EXCL, 0600);
	if (fd < 0) {
		fprintf(stderr, "can't create %s: %s\n", out_path, strerror(errno));
		return WRITE_FAILED;
	}
	out.file = fdopen(fd, "wb");
	if (!out.file) {
		close(fd);
		unlink(out_path);
		fprintf(stderr, "can't write %s\n", out_path);
		return WRITE_FAILED;
	}

	struct sigaction action;
	memset(&action, 0, sizeof action);
	action.sa_handler = on_signal;
	sigemptyset(&action.sa_mask);
	sigaction(SIGINT, &action, NULL);
	sigaction(SIGTERM, &action, NULL);
	signal(SIGPIPE, SIG_IGN);

	write_section_header(&out);

	int code = DONE;
	if (stream_path) {
		FILE *stream = fopen(stream_path, "rb");
		if (!stream) {
			fprintf(stderr, "can't read %s: %s\n", stream_path, strerror(errno));
			code = BAD_STREAM;
		} else {
			uint8_t prefix[4];
			uint8_t *message = malloc(MAX_MESSAGE);
			while (message && !stop_requested && fread(prefix, 1, 4, stream) == 4) {
				uint32_t length = be32(prefix);
				if (length > MAX_MESSAGE || fread(message, 1, length, stream) != length) {
					fprintf(stderr, "%s is cut short or has a message that is too long\n", stream_path);
					code = BAD_STREAM;
					break;
				}
				if (handle(&out, message, length, 0, 1) < 0)
					break;
			}
			free(message);
			fclose(stream);
		}
		code = finish(&out, code);
		if (code != DONE && code != WRITE_FAILED)
			unlink(out_path);
		return code;
	}

	idevice_t device = NULL;
	/* --network: only over Wi-Fi, even when the same iPhone is also on a cable. Else the cable first. */
	enum idevice_options lookup = network_only ? IDEVICE_LOOKUP_NETWORK : (IDEVICE_LOOKUP_USBMUX | IDEVICE_LOOKUP_NETWORK);
	if (idevice_new_with_options(&device, udid, lookup) != IDEVICE_E_SUCCESS || !device) {
		fprintf(stderr, "no iPhone with that UDID is connected\n");
		finish(&out, DONE);
		unlink(out_path);
		return NO_DEVICE;
	}
	lockdownd_client_t client = NULL;
	lockdownd_error_t error = lockdownd_client_new_with_handshake(device, &client, "mobster-pcap");
	if (error != LOCKDOWN_E_SUCCESS) {
		fprintf(stderr, error == LOCKDOWN_E_PASSWORD_PROTECTED
		                ? "the iPhone is locked: unlock it, then try again\n"
		                : "the iPhone doesn't trust this Mac (lockdown error %d): unlock it and tap Trust\n", error);
		idevice_free(device);
		finish(&out, DONE);
		unlink(out_path);
		return handshake_code(error);
	}
	lockdownd_service_descriptor_t service = NULL;
	error = lockdownd_start_service(client, SERVICE, &service);
	lockdownd_client_free(client);
	if (error != LOCKDOWN_E_SUCCESS || !service) {
		if (network_only && error == LOCKDOWN_E_INVALID_SERVICE)
			/* iOS 26 offers com.apple.pcapd over the cable only (measured on an iPhone 15 Pro, 15 Oct 2026). */
			fprintf(stderr, "iOS doesn't offer packet capture over Wi-Fi: plug the iPhone in, then try again\n");
		else
			fprintf(stderr, "the iPhone refused the packet capture service (lockdown error %d)\n", error);
		idevice_free(device);
		finish(&out, DONE);
		unlink(out_path);
		return error == LOCKDOWN_E_PASSWORD_PROTECTED ? NOT_TRUSTED : REFUSED;
	}
	/* Over Wi-Fi the capture's metadata must travel encrypted: refuse before a byte is read, never fall back to clear. */
	if (network_only && !service->ssl_enabled) {
		fprintf(stderr, "the iPhone offered the packet capture service without encryption, so Mobster won't capture it over Wi-Fi: use a cable\n");
		lockdownd_service_descriptor_free(service);
		idevice_free(device);
		finish(&out, DONE);
		unlink(out_path);
		return REFUSED;
	}
	property_list_service_client_t pcapd = NULL;
	if (property_list_service_client_new(device, service, &pcapd) != PROPERTY_LIST_SERVICE_E_SUCCESS || !pcapd) {
		fprintf(stderr, "couldn't connect to the iPhone's packet capture service\n");
		lockdownd_service_descriptor_free(service);
		idevice_free(device);
		finish(&out, DONE);
		unlink(out_path);
		return BROKEN;
	}
	if (service->ssl_enabled)
		property_list_service_enable_ssl(pcapd);
	lockdownd_service_descriptor_free(service);

	double started = monotonic(), last_flush = started;
	uint64_t packets = 0;
	while (!stop_requested && code == DONE) {
		if (seconds && monotonic() - started >= (double)seconds)
			break;
		plist_t message = NULL;
		property_list_service_error_t result = property_list_service_receive_plist_with_timeout(pcapd, &message, 500);
		double t = monotonic();
		if (result == PROPERTY_LIST_SERVICE_E_RECEIVE_TIMEOUT) {
			if (t - last_flush >= 1.0 && fflush(out.file) == 0)
				last_flush = t;
			continue;
		}
		if (result != PROPERTY_LIST_SERVICE_E_SUCCESS || !message) {
			if (!stop_requested) {
				fprintf(stderr, "lost the connection to the iPhone\n");
				code = BROKEN;
			}
			break;
		}
		if (plist_get_node_type(message) == PLIST_DATA) {
			char *data = NULL;
			uint64_t size = 0;
			plist_get_data_val(message, &data, &size);
			if (data) {
				int status = size <= MAX_MESSAGE ? handle(&out, (const uint8_t *)data, (size_t)size, now(), 0) : 0;
				free(data);
				if (status > 0)
					packets++;
				if (status < 0 || out.failed) {
					plist_free(message);
					break;
				}
			}
		}
		plist_free(message);
		if (t - last_flush >= 1.0 && fflush(out.file) == 0)
			last_flush = t;
	}
	property_list_service_client_free(pcapd);
	idevice_free(device);
	code = finish(&out, code);
	if (code == DONE && packets == 0)
		fprintf(stderr, "no packets arrived: the phone sent nothing in that time\n");
	return code;
}
