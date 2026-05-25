from resume.resume.doctype.pdf_upload.pdf_upload import _extract_and_parse_file
from vaaman_ats_ai.api.resume.resume import (
    calculate_experience_years,
    flatten_resume_data,
    create_resume_from_upload,
    match_job_opening_hybrid
)

import os
import json
import frappe

import re

def clean_phone_number(phone):
    if not phone:
        return ""

    # Convert list to string if AI returns array
    if isinstance(phone, list):
        phone = ",".join(phone)

    # Split multiple numbers
    numbers = re.split(r"[,\n;/]+", str(phone))

    # Take first valid number
    for num in numbers:
        num = num.strip()

        # Remove unwanted chars
        num = re.sub(r"[^\d+]", "", num)

        # Basic validation
        if len(re.sub(r"\D", "", num)) >= 10:
            return num

    return ""


@frappe.whitelist(allow_guest=True)
def fetch_email_resumes():

    # ✅ Get configured email account
    email_account = (
        frappe.db.get_single_value("ATS Settings", "career_email_account")
        or frappe.conf.get("email_account")
    )

    if not email_account:
        frappe.log_error(
            title="Email Account Not Configured",
            message="Career email account not configured in ATS Settings."
        )
        return {
            "status": "error",
            "message": "Email account not configured"
        }

    # ✅ Fetch only limited unprocessed emails
    communications = frappe.get_all(
        "Communication",
        filters={
            "communication_type": "Communication",
            "sent_or_received": "Received",
            "email_account": email_account,
            "custom_processed": 0,
            "custom_processing": 0
        },
        fields=["name", "subject", "sender"],
        limit_page_length=10,
        order_by="creation desc"
    )

    if not communications:
        return {
            "status": "success",
            "message": "No new emails found"
        }

    # ✅ Fetch active job openings once
    active_job_openings = frappe.get_all(
        "Job Opening",
        filters={"status": "Open"},
        fields=["name", "job_title", "department", "description"]
    )

    queued = 0

    for comm in communications:
        try:
            # ✅ Lock record immediately
            frappe.db.set_value(
                "Communication",
                comm.name,
                "custom_processing",
                1
            )

            frappe.db.commit()
            
            frappe.enqueue(
                "vaaman_ats_ai.api.email.fetch_resumes.process_single_email_resume",
                queue="long",
                communication_name=comm.name,
                job_openings=active_job_openings
            )

            queued += 1

        except Exception:
            frappe.log_error(
                title="Queue Resume Processing Failed",
                message=frappe.get_traceback()
            )

    return {
        "status": "success",
        "queued": queued
    }


def process_single_email_resume(communication_name, job_openings=None):

    try:
        if not frappe.db.exists("Communication", communication_name):
            frappe.log_error(
                title="Communication Missing",
                message=f"Communication {communication_name} no longer exists."
            )
            return

        comm_doc = frappe.get_doc("Communication", communication_name)

        # ✅ Skip already processed
        if comm_doc.custom_processed:
            return

        email_subject = comm_doc.subject or ""

        # ✅ Limit email body size to avoid memory issues
        email_body = (comm_doc.content or "")[:5000]

        # ✅ Get attachments
        files = frappe.get_all(
            "File",
            filters={"attached_to_name": communication_name},
            fields=["name", "file_url", "file_name"]
        )

        # ✅ Delete emails without attachments
        if not files:
            frappe.delete_doc(
                "Communication",
                communication_name,
                ignore_permissions=True
            )
            frappe.db.commit()
            return

        # ✅ Filter valid resume files
        valid_files = [
            f for f in files
            if f.file_name.lower().endswith((".pdf", ".doc", ".docx"))
        ]

        if not valid_files:
            frappe.delete_doc(
                "Communication",
                communication_name,
                ignore_permissions=True
            )
            frappe.db.commit()
            return

        # ✅ Process each attachment
        for f in valid_files:

            try:

                matched_job_id = None

                # ✅ AI Job Matching
                if job_openings:
                    matched_job_id = match_job_opening_hybrid(
                        email_subject=email_subject,
                        email_body=email_body,
                        job_openings=job_openings
                    )

                frappe.log_error(
                    title="AI Job Matching",
                    message=f"""
                    Email Subject: {email_subject}

                    Matched Job:
                    {matched_job_id}
                    """
                )

                # ✅ Get file path
                file_doc = frappe.get_doc(
                    "File",
                    {"file_url": f.file_url}
                )

                file_path = file_doc.get_full_path()

                ext = os.path.splitext(file_path)[1].lower()

                # ✅ Load prompt template
                prompt_path = frappe.get_app_path(
                    "resume",
                    "resume",
                    "doctype",
                    "pdf_upload",
                    "resume_prompt.txt"
                )

                with open(prompt_path, "r") as pf:
                    prompt_template = pf.read()

                api_key = frappe.conf.get("gemini_api_key")

                # ✅ Parse Resume
                _fu, applicant_data, err = _extract_and_parse_file((
                    file_path,
                    f.file_url,
                    None,
                    None,
                    ext,
                    api_key,
                    prompt_template,
                ))

                if isinstance(applicant_data, str):
                    try:
                        applicant_data = json.loads(applicant_data)
                    except Exception:
                        continue

                if err or not applicant_data:
                    continue

                # ✅ Normalize fields
                if (
                    "email_id" in applicant_data
                    and "email" not in applicant_data
                ):
                    applicant_data["email"] = applicant_data["email_id"]

                if (
                    "phone_number" in applicant_data
                    and "phone" not in applicant_data
                ):
                    applicant_data["phone"] = applicant_data["phone_number"]

                applicant_name = (
                    applicant_data.get("applicant_name")
                    or applicant_data.get("name")
                    or applicant_data.get("full_name")
                )

                email_value = applicant_data.get("email")

                if not applicant_name or not email_value:
                    continue

                # ✅ Skip duplicate applicants
                if frappe.db.exists(
                    "Job Applicant",
                    {"email_id": email_value}
                ):
                    continue

                # ✅ Calculate experience
                applicant_data["experience_years"] = (
                    calculate_experience_years(
                        applicant_data.get("experience", [])
                    )
                )

                flat_data = flatten_resume_data(applicant_data)
                
                clean_phone = clean_phone_number(
                    applicant_data.get("phone", "")
                )

                # ✅ Create Job Applicant
                applicant = frappe.get_doc({
                    "doctype": "Job Applicant",
                    "applicant_name": applicant_name,
                    "email_id": email_value,
                    "job_title": (
                        matched_job_id.get("job_opening")
                        if matched_job_id
                        else None
                    ),
                    "resume_attachment": f.file_url,
                    "status": "Open",
                    # "phone_number": applicant_data.get("phone", ""),
                    "phone_number": clean_phone,
                    "custom_parsed_json": json.dumps(applicant_data),
                    "custom_parse_status": "Parsed",
                    "custom_experience_years": flat_data.get(
                        "experience_years", 0
                    ),
                    "current_location": flat_data.get(
                        "location", ""
                    ),
                    "custom_skills": flat_data.get(
                        "skills", ""
                    ),
                    "custom_current_role": flat_data.get(
                        "current_role", ""
                    ),
                    "custom_degree": flat_data.get(
                        "degree", ""
                    ),
                    "custom_institution": flat_data.get(
                        "institution", ""
                    ),
                })

                applicant.insert(ignore_permissions=True)

                frappe.db.commit()

                # ✅ Create embeddings
                try:
                    create_resume_from_upload(
                        applicant_data=applicant_data,
                        file_url=f.file_url,
                        applicant_doc=applicant
                    )

                except Exception:
                    frappe.log_error(
                        title=f"Resume Embedding Failed: {applicant.name}",
                        message=frappe.get_traceback()
                    )

            except Exception:
                frappe.log_error(
                    title="Resume Processing Failed",
                    message=frappe.get_traceback()
                )

        # ✅ Mark email processed
        frappe.db.set_value(
            "Communication",
            communication_name,
            {
                "custom_processed": 1,
                "custom_processing": 0
            }
        )

        frappe.db.commit()

    except Exception:

        if frappe.db.exists("Communication", communication_name):

            frappe.db.set_value(
                "Communication",
                communication_name,
                "custom_processing",
                0
            )

            frappe.db.commit()

        frappe.log_error(
            title="Email Resume Fetch Failed",
            message=frappe.get_traceback()
        )