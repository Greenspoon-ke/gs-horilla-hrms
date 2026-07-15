from django.core.management.base import BaseCommand

from base.models import Company, HorillaMailTemplate

TEMPLATE_TITLE = "HRMS Welcome Invite"

TEMPLATE_BODY = """
<!DOCTYPE html>
<html>
<body style="margin:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">
          <tr>
            <td style="background:#008800;padding:24px 30px;text-align:center;">
              <div style="color:#ffffff;font-size:20px;font-weight:bold;">GreenSpoon Limited</div>
              <div style="color:#e8ffe8;font-size:13px;margin-top:4px;">Human Resource Management System</div>
            </td>
          </tr>
          <tr>
            <td style="padding:32px 30px;color:#333;font-size:15px;line-height:1.6;">
              <p style="margin:0 0 16px;">Hi {{ instance.employee_first_name }},</p>
              <p style="margin:0 0 16px;">Your <strong>GreenSpoon Limited HRMS</strong> account is ready. Use the steps below to sign in for the first time.</p>
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fafafa;border:1px solid #eee;border-radius:6px;margin:20px 0;">
                <tr><td style="padding:16px;">
                  <strong>First-time sign in</strong><br><br>
                  1. Open the login page<br>
                  2. Enter your <strong>work email</strong> as username<br>
                  3. Click <strong>Forgot password?</strong><br>
                  4. Set your password from the email link<br>
                  5. Sign in again with your new password
                </td></tr>
              </table>
              <table cellpadding="0" cellspacing="0" style="margin:24px auto;">
                <tr>
                  <td style="background:#008800;border-radius:8px;">
                    <a href="https://hr.greenspoon.co.ke/login/" style="display:inline-block;padding:14px 28px;color:#fff;text-decoration:none;font-weight:bold;">Go to Login</a>
                  </td>
                  <td width="12"></td>
                  <td style="border:1px solid #008800;border-radius:8px;">
                    <a href="https://hr.greenspoon.co.ke/forgot-password" style="display:inline-block;padding:14px 28px;color:#008800;text-decoration:none;font-weight:bold;">Reset Password</a>
                  </td>
                </tr>
              </table>
              <p style="margin:0;font-size:13px;color:#666;">
                Login: <a href="https://hr.greenspoon.co.ke/login/" style="color:#008800;">https://hr.greenspoon.co.ke/login/</a><br>
                Forgot password: <a href="https://hr.greenspoon.co.ke/forgot-password" style="color:#008800;">https://hr.greenspoon.co.ke/forgot-password</a>
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 30px;background:#fafafa;text-align:center;font-size:12px;color:#888;">
              &copy; GreenSpoon Limited · HR Team<br>
              If you need help, contact your HR administrator.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
""".strip()


class Command(BaseCommand):
    help = (
        "Create or update the HRMS Welcome Invite mail template "
        "for the employee Send Mail dropdown."
    )

    def handle(self, *args, **options):
        hq = Company.objects.filter(hq=True).first()
        template, created = HorillaMailTemplate.objects.entire().update_or_create(
            title=TEMPLATE_TITLE,
            defaults={
                "body": TEMPLATE_BODY,
                "company_id": hq,
            },
        )
        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f'{action} mail template "{template.title}" (id={template.id}).'
            )
        )
