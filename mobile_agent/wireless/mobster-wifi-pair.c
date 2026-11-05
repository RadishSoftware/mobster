/*
 * mobster-wifi-pair: let a trusted iPhone be reached over Wi-Fi, or stop it.
 *
 * It sets the lockdown value com.apple.mobile.wireless_lockdown/EnableWifiConnections, the same flag as Xcode's
 * "Connect via network". Only over USB: the phone must be plugged in and trust this Mac. It never pairs a new
 * Mac, never enters a passcode and changes nothing else on the phone.
 *
 *   mobster-wifi-pair --udid UDID --enable | --disable | --status
 *
 * Prints one JSON line, {"enabled": true|false}, and exits:
 *   0 done, 2 usage, 3 no iPhone with that UDID on USB, 4 the iPhone doesn't trust this Mac (or is locked),
 *   5 the iPhone refused the change, 6 anything else.
 *
 * This file is MIT, like Mobster. desktop/scripts/build-iphone-tools.sh compiles it against the static
 * libimobiledevice and libplist it bundles (LGPL-2.1, like the iPhone tools already in the app).
 */
#include <stdio.h>
#include <string.h>

#include <libimobiledevice/libimobiledevice.h>
#include <libimobiledevice/lockdown.h>
#include <plist/plist.h>

#define DOMAIN "com.apple.mobile.wireless_lockdown"
#define KEY "EnableWifiConnections"

enum { DONE = 0, USAGE = 2, NO_DEVICE = 3, NOT_TRUSTED = 4, REFUSED = 5, FAILED = 6 };

static int usage(void)
{
	fprintf(stderr, "usage: mobster-wifi-pair --udid UDID --enable | --disable | --status\n");
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
		return FAILED;
	}
}

int main(int argc, char **argv)
{
	const char *udid = NULL;
	int action = 0; /* 'e' enable, 'd' disable, 's' status */
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--udid") && i + 1 < argc)
			udid = argv[++i];
		else if (!action && !strcmp(argv[i], "--enable"))
			action = 'e';
		else if (!action && !strcmp(argv[i], "--disable"))
			action = 'd';
		else if (!action && !strcmp(argv[i], "--status"))
			action = 's';
		else
			return usage();
	}
	if (!udid || !action || !valid_udid(udid))
		return usage();

	idevice_t device = NULL;
	/* USB only: the flag is turned on with the cable, never over the network. */
	if (idevice_new_with_options(&device, udid, IDEVICE_LOOKUP_USBMUX) != IDEVICE_E_SUCCESS || !device) {
		fprintf(stderr, "no iPhone with that UDID is on USB\n");
		return NO_DEVICE;
	}
	lockdownd_client_t client = NULL;
	lockdownd_error_t error = lockdownd_client_new_with_handshake(device, &client, "mobster-wifi-pair");
	if (error != LOCKDOWN_E_SUCCESS) {
		fprintf(stderr, "lockdown handshake failed (%d)\n", error);
		idevice_free(device);
		return handshake_code(error);
	}
	int code = DONE;
	if (action != 's') {
		/* lockdownd_set_value takes the node and frees it. */
		error = lockdownd_set_value(client, DOMAIN, KEY, plist_new_bool(action == 'e'));
		if (error != LOCKDOWN_E_SUCCESS) {
			fprintf(stderr, "the iPhone refused the change (%d)\n", error);
			code = error == LOCKDOWN_E_SET_PROHIBITED || error == LOCKDOWN_E_IMMUTABLE_VALUE ? REFUSED : FAILED;
		}
	}
	if (code == DONE) {
		plist_t value = NULL;
		uint8_t enabled = 0;
		error = lockdownd_get_value(client, DOMAIN, KEY, &value);
		if (error == LOCKDOWN_E_SUCCESS && value && plist_get_node_type(value) == PLIST_BOOLEAN)
			plist_get_bool_val(value, &enabled);
		else if (error != LOCKDOWN_E_SUCCESS && error != LOCKDOWN_E_MISSING_VALUE && error != LOCKDOWN_E_MISSING_KEY)
			code = FAILED;
		if (value)
			plist_free(value);
		if (code == DONE)
			printf("{\"enabled\": %s}\n", enabled ? "true" : "false");
	}
	lockdownd_client_free(client);
	idevice_free(device);
	return code;
}
