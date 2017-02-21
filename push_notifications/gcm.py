"""
Google Cloud Messaging
Previously known as C2DM
Documentation is available on the Android Developer website:
https://developer.android.com/google/gcm/index.html
"""

import json
from .models import GCMDevice


try:
	from urllib.request import Request, urlopen
except ImportError:
	# Python 2 support
	from urllib2 import Request, urlopen

from django.core.exceptions import ImproperlyConfigured
from . import NotificationError
from .settings import PUSH_NOTIFICATIONS_SETTINGS as SETTINGS


# Valid keys for FCM messages
# See ref : https://firebase.google.com/docs/cloud-messaging/http-server-ref#notification-payload-support
FCM_TARGETS_KEYS = [
	'to', 'condition', 'notification_key'
]
FCM_OPTIONS_KEYS = [
	'collapse_key', 'priority', 'content_available', 'delay_while_idle', 'time_to_live',
	'restricted_package_name', 'dry_run'
]
FCM_NOTIFICATIONS_PAYLOAD_KEYS = [
	'title', 'body', 'icon', 'sound', 'badge', 'color', 'tag', 'click_action',
	'body_loc_key', 'body_loc_args', 'title_loc_key', 'title_loc_args'
]


class GCMError(NotificationError):
	pass


def _chunks(l, n):
	"""
	Yield successive chunks from list \a l with a minimum size \a n
	"""
	for i in range(0, len(l), n):
		yield l[i:i + n]


def _gcm_send(data, content_type):
	key = SETTINGS.get("GCM_API_KEY")
	if not key:
		raise ImproperlyConfigured(
			'You need to set PUSH_NOTIFICATIONS_SETTINGS["GCM_API_KEY"] to send messages through GCM.'
		)

	headers = {
		"Content-Type": content_type,
		"Authorization": "key=%s" % (key),
		"Content-Length": str(len(data)),
	}
	request = Request(SETTINGS["GCM_POST_URL"], data, headers)
	return urlopen(request, timeout=SETTINGS["GCM_ERROR_TIMEOUT"]).read().decode("utf-8")


def _fcm_send(data, content_type):
	key = SETTINGS.get("FCM_API_KEY")
	if not key:
		raise ImproperlyConfigured(
			'You need to set PUSH_NOTIFICATIONS_SETTINGS["FCM_API_KEY"] to send messages through FCM.'
		)

	headers = {
		"Content-Type": content_type,
		"Authorization": "key=%s" % (key),
		"Content-Length": str(len(data)),
	}
	request = Request(SETTINGS["FCM_POST_URL"], data, headers)
	return urlopen(request, timeout=SETTINGS["FCM_ERROR_TIMEOUT"]).read().decode("utf-8")


def _cm_handle_response(registration_ids, response_data, cloud_type):
	response = response_data
	if response.get("failure") or response.get("canonical_ids"):
		ids_to_remove, old_new_ids = [], []
		throw_error = False
		for index, result in enumerate(response["results"]):
			error = result.get("error")
			if error:
				# Information from Google docs
				# https://developers.google.com/cloud-messaging/http
				# If error is NotRegistered or InvalidRegistration,
				# then we will deactivate devices because this
				# registration ID is no more valid and can't be used
				# to send messages, otherwise raise error
				if error in ("NotRegistered", "InvalidRegistration"):
					ids_to_remove.append(registration_ids[index])
				else:
					throw_error = True

			# If registration_id is set, replace the original ID with
			# the new value (canonical ID) in your server database.
			# Note that the original ID is not part of the result, so
			# you need to obtain it from the list of registration_ids
			# passed in the request (using the same index).
			new_id = result.get("registration_id")
			if new_id:
				old_new_ids.append((registration_ids[index], new_id))

		if ids_to_remove:
			removed = GCMDevice.objects.filter(registration_id__in=ids_to_remove, cloud_message_type=cloud_type)
			removed.update(active=0)

		for old_id, new_id in old_new_ids:
			_gcm_handle_canonical_id(new_id, old_id, cloud_type)

		if throw_error:
			raise GCMError(response)
	return response


def _cm_send_request(registration_ids, data, cloud_type="GCM", use_fcm_notifications=True, **kwargs):
	"""
	Sends a GCM notification to one or more registration_ids. The registration_ids
	needs to be a list.
	This will send the notification as json data.
	"""

	payload = {"registration_ids": registration_ids} if registration_ids else {}

	# If using FCM, optionnally autodiscovers notification related keys
	# https://firebase.google.com/docs/cloud-messaging/concept-options#notifications_and_data_messages
	if cloud_type == "FCM" and use_fcm_notifications:
		notification_payload = {}
		if 'message' in data:
			notification_payload['body'] = data.pop('message', None)

		for key in FCM_NOTIFICATIONS_PAYLOAD_KEYS:
			value_from_extra = data.pop(key, None)
			if value_from_extra:
				notification_payload[key] = value_from_extra
			value_from_kwargs = kwargs.pop(key, None)
			if value_from_kwargs:
				notification_payload[key] = value_from_kwargs
		if notification_payload:
			payload['notification'] = notification_payload

	if data:
		payload['data'] = data

	# Attach any additional non falsy keyword args (targets, options)
	# See ref : https://firebase.google.com/docs/cloud-messaging/http-server-ref#table1
	payload.update({k: v for k, v in kwargs.items() if v and (k in FCM_TARGETS_KEYS or k in FCM_OPTIONS_KEYS)})

	# Sort the keys for deterministic output (useful for tests)
	json_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

	if cloud_type == "GCM":
		response = json.loads(_gcm_send(json_payload, "application/json"))
	elif cloud_type == "FCM":
		response = json.loads(_fcm_send(json_payload, "application/json"))
	else:
		raise ImproperlyConfigured("cloud_type must be GCM or FCM not %s" % str(cloud_type))
	return _cm_handle_response(registration_ids, response, cloud_type)


def _gcm_handle_canonical_id(canonical_id, current_id, cloud_type):
	"""
	Handle situation when GCM server response contains canonical ID
	"""
	if GCMDevice.objects.filter(registration_id=canonical_id, cloud_message_type=cloud_type, active=True).exists():
		GCMDevice.objects.filter(registration_id=current_id, cloud_message_type=cloud_type).update(active=False)
	else:
		GCMDevice.objects.filter(registration_id=current_id, cloud_message_type=cloud_type)\
			.update(registration_id=canonical_id)


def send_message(registration_id, data, cloud_type, **kwargs):
	"""
	Sends a GCM or FCM notification to a single registration_id.

	If sending multiple notifications, it is more efficient to use
	send_bulk_message() with a list of registration_ids

	A reference of extra keyword arguments sent to the server is available here:
	https://developers.google.com/cloud-messaging/server-ref#downstream
	"""

	if registration_id:
		return _cm_send_request([registration_id], data, cloud_type, **kwargs)


def send_bulk_message(registration_ids, data, cloud_type, **kwargs):
	"""
	Sends a GCM or FCM notification to one or more registration_ids. The registration_ids
	needs to be a list.
	This will send the notification as json data.

	A reference of extra keyword arguments sent to the server is available here:
	https://firebase.google.com/docs/cloud-messaging/send-message
	"""
	if cloud_type == "GCM":
		max_recipients = SETTINGS.get("GCM_MAX_RECIPIENTS")
	elif cloud_type == "FCM":
		max_recipients = SETTINGS.get("FCM_MAX_RECIPIENTS")
	else:
		raise ImproperlyConfigured("cloud_type must be GCM or FCM not %s" % str(cloud_type))

	if registration_ids is None and "/topics/" not in kwargs.get("to", ""):
		return
	# GCM only allows up to 1000 reg ids per bulk message
	# https://developer.android.com/google/gcm/gcm.html#request
	if registration_ids:
		if len(registration_ids) > max_recipients:
			ret = []
			for chunk in _chunks(registration_ids, max_recipients):
				ret.append(_cm_send_request(chunk, data, cloud_type=cloud_type, **kwargs))
			return ret

	return _cm_send_request(registration_ids, data, cloud_type=cloud_type, **kwargs)
