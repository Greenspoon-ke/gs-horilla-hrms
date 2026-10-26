"""
Guards for the Django 4.2.30 pin (tracker/plans/p1-05-django-4-2-30.md).

They fail if the pin is lowered, and record the behaviour changes users can
see. No database access: SimpleTestCase refuses queries.
"""

import django
from django import forms
from django.http import HttpResponse
from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils.log import log_response


class DjangoPatchLevelTests(SimpleTestCase):
    def test_django_is_4_2_30_or_later(self):
        self.assertGreaterEqual(django.VERSION[:3], (4, 2, 30))

    def test_log_response_escapes_control_characters(self):
        # CVE-2025-48432: a CR/LF in the path must not start a new log line.
        with self.assertLogs("django.request", "WARNING") as logs:
            log_response(
                "Not Found: %s", "/nope/\r\nFAKE", response=HttpResponse(status=404)
            )
        self.assertIn("/nope/\\r\\nFAKE", logs.output[0])
        self.assertNotIn("\n", logs.output[0])

    def test_truncatechars_html_wellformed_rich_text_unchanged(self):
        out = Template("{{ v|safe|truncatechars_html:30 }}").render(
            Context(
                {
                    "v": "<p>Hello <b>world</b> this is a long resignation letter text</p>"
                }
            )
        )
        self.assertEqual(out, "<p>Hello <b>world</b> this is a long re…</p>")

    def test_urlfield_still_adds_http_scheme(self):
        self.assertEqual(
            forms.URLField().clean("linkedin.com/in/jane-doe"),
            "http://linkedin.com/in/jane-doe",
        )

    def test_urlfield_rejects_embedded_newline(self):
        # 4.2.29: a line break inside a pasted URL is no longer silently removed.
        with self.assertRaises(forms.ValidationError):
            forms.URLField().clean("http://exa\nmple.com")
