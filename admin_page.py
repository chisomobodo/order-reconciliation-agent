"""
Arbiter's admin page -- User Management, Signup Access, Permission
Mapping, and the Audit Log, all built directly from rbac.py. Routed to via app.py's
st.navigation() ONLY when the current user's role is 'admin' -- see
app.py for how the page list itself is built. That's what actually
keeps this page out of a non-admin's reach: it isn't in the navigation
object's page list at all for their session, not merely hidden by CSS
or a disabled widget, and Streamlit falls back to the default page for
any URL that doesn't match a page in the CURRENT run's list -- so
typing the admin URL directly doesn't reach this function's code for a
non-admin either.

The has_permission() check at the top of render_admin_page() is a
second, defense-in-depth layer on top of that role-based routing: it
only matters in the edge case where an admin edits the Permission
Mapping grid below to remove can_manage_users from the 'admin' role
itself, which the role-based nav check alone wouldn't catch (nav is
keyed on role == 'admin', not on this specific permission).
"""
import html
from contextlib import closing

import pandas as pd
import streamlit as st

import app_core
import rbac
from theme import empty_state_html, section_heading_html


def render_admin_page(current_user: dict):
    st.markdown(app_core.render_app_header(), unsafe_allow_html=True)
    st.markdown(
        '<div class="auth-form-caption" style="margin-bottom: 1rem;">Admin — user roles, permissions, and the audit trail.</div>',
        unsafe_allow_html=True,
    )

    with closing(app_core.auth_get_connection()) as _perm_conn:
        can_manage_users = rbac.has_permission(_perm_conn, current_user["user_id"], "can_manage_users")

    if not can_manage_users:
        st.error(
            "Your role no longer has the 'can_manage_users' permission, so this page can't do "
            "anything for you right now. Ask another admin to restore it in Permission Mapping below "
            "-- or, if no admin can reach this page anymore, add your email to ADMIN_EMAILS and log "
            "in again; that always re-grants the admin role on login."
        )
        return

    # =====================================================================
    # User Management
    # =====================================================================
    st.markdown(section_heading_html("User Management"), unsafe_allow_html=True)

    with closing(app_core.auth_get_connection()) as conn:
        users = rbac.get_all_users(conn)

    if not users:
        st.markdown(empty_state_html("No users yet."), unsafe_allow_html=True)
    else:
        header_col1, header_col2, header_col3 = st.columns([3, 1.6, 1])
        with header_col1:
            st.caption("USER")
        with header_col2:
            st.caption("ROLE")
        with header_col3:
            st.caption("")

        for u in users:
            is_self = u["id"] == current_user["user_id"]
            role_options = list(rbac.VALID_ROLES)

            info_col, role_col, save_col = st.columns([3, 1.6, 1])
            with info_col:
                label = html.escape(u["name"] or u["email"])
                st.markdown(f"**{label}**{' *(you)*' if is_self else ''}")
                st.caption(f"{html.escape(u['email'])} — joined {u['created_at']}")
            with role_col:
                selected_role = st.selectbox(
                    "Role",
                    options=role_options,
                    index=role_options.index(u["role"]) if u["role"] in role_options else role_options.index(rbac.DEFAULT_ROLE),
                    key=f"role_select_{u['id']}",
                    label_visibility="collapsed",
                )
            with save_col:
                changed = selected_role != u["role"]
                if st.button("Save", key=f"role_save_{u['id']}", use_container_width=True, disabled=not changed):
                    with closing(app_core.auth_get_connection()) as write_conn:
                        result = rbac.set_user_role(
                            write_conn, u["id"], selected_role,
                            actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                        )
                    if result.get("status") == "Success":
                        st.toast(result["message"], icon="✅")
                    else:
                        st.toast(result.get("message", "Failed to update role."), icon="⚠️")
                    st.rerun()
            if is_self and changed:
                st.caption("⚠️ This changes your own role. If you remove your own admin access, ADMIN_EMAILS + logging in again always restores it.")
            st.divider()

    # =====================================================================
    # Signup Access
    # =====================================================================
    st.markdown(section_heading_html("Signup Access"), unsafe_allow_html=True)
    st.caption(
        "Who can create an account. Adding an email here takes effect immediately -- no env var "
        "change or restart needed. ALLOWED_SIGNUP_EMAILS (if set) still works as a permanent "
        "fallback on top of this list."
    )

    # A toast alone isn't enough here: the outcome can be a long,
    # important sentence ("...but the notification email failed to
    # send: <SMTP error>"), not just a quick "done" confirmation, and
    # toasts disappear on their own after a few seconds. Held in
    # session_state and rendered inline (same pattern app.py's own
    # login/signup screen uses for its messages) so it stays visible
    # across the st.rerun() below until the next add/revoke replaces it.
    if "signup_access_message" not in st.session_state:
        st.session_state.signup_access_message = None

    with st.form("add_allowed_signup_email_form", clear_on_submit=True):
        add_col, add_btn_col = st.columns([4, 1])
        with add_col:
            new_allowed_email = st.text_input(
                "Email to allow", placeholder="newperson@example.com", label_visibility="collapsed",
            )
        with add_btn_col:
            add_submitted = st.form_submit_button("Add email", type="primary", use_container_width=True)
        should_notify = st.checkbox("Also send them a notification email", value=True)
    if add_submitted:
        if not new_allowed_email.strip():
            st.session_state.signup_access_message = ("Enter an email address first.", "error")
        else:
            with closing(app_core.auth_get_connection()) as write_conn:
                result = rbac.add_allowed_signup_email(
                    write_conn, new_allowed_email.strip(), notify=should_notify,
                    actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                )
            kind = "success" if result.get("status") == "Success" else "error"
            st.session_state.signup_access_message = (result.get("message", "Failed to add email."), kind)
        st.rerun()

    if st.session_state.signup_access_message:
        text, kind = st.session_state.signup_access_message
        if kind == "success":
            st.success(text)
        else:
            st.error(text)

    with closing(app_core.auth_get_connection()) as conn:
        allowed_emails = rbac.get_allowed_signup_emails(conn)

    if not allowed_emails:
        st.markdown(
            empty_state_html("No emails on the database allowlist yet -- signup is open to anyone (or gated only by ALLOWED_SIGNUP_EMAILS, if set)."),
            unsafe_allow_html=True,
        )
    else:
        for entry in allowed_emails:
            entry_col, revoke_col = st.columns([4, 1])
            with entry_col:
                st.markdown(f"**{html.escape(entry['email'])}**")
                st.caption(f"added by {html.escape(entry['added_by_email'])} — {entry['added_at']}")
            with revoke_col:
                if st.button("Revoke", key=f"revoke_signup_email_{entry['email']}", use_container_width=True):
                    with closing(app_core.auth_get_connection()) as write_conn:
                        result = rbac.remove_allowed_signup_email(
                            write_conn, entry["email"],
                            actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                        )
                    st.session_state.signup_access_message = None
                    st.toast(result.get("message", "Removed."), icon="🚫")
                    st.rerun()
            st.divider()

    # =====================================================================
    # Permission Mapping
    # =====================================================================
    st.markdown(section_heading_html("Permission Mapping"), unsafe_allow_html=True)
    st.caption("Toggling a box takes effect immediately.")

    with closing(app_core.auth_get_connection()) as conn:
        role_permissions = rbac.get_all_role_permissions(conn)

    perm_header_cols = st.columns([1.4] + [1.6] * len(rbac.ALL_PERMISSIONS))
    with perm_header_cols[0]:
        st.caption("ROLE")
    for col, permission in zip(perm_header_cols[1:], rbac.ALL_PERMISSIONS):
        with col:
            st.caption(permission.replace("can_", "").replace("_", " ").upper())

    for role in rbac.VALID_ROLES:
        row_cols = st.columns([1.4] + [1.6] * len(rbac.ALL_PERMISSIONS))
        with row_cols[0]:
            st.markdown(f"**{role.capitalize()}**")
        for col, permission in zip(row_cols[1:], rbac.ALL_PERMISSIONS):
            with col:
                current_value = role_permissions.get(role, {}).get(permission, False)
                new_value = st.checkbox(
                    permission, value=current_value,
                    key=f"perm_{role}_{permission}", label_visibility="collapsed",
                )
                if new_value != current_value:
                    with closing(app_core.auth_get_connection()) as write_conn:
                        rbac.set_role_permission(
                            write_conn, role, permission, new_value,
                            actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                        )
                    st.rerun()
    st.divider()

    # =====================================================================
    # Audit Log
    # =====================================================================
    st.markdown(section_heading_html("Audit Log"), unsafe_allow_html=True)

    with closing(app_core.auth_get_connection()) as conn:
        audit_rows = rbac.get_audit_log(conn, is_azure=app_core.USE_AZURE_DB)

    if not audit_rows:
        st.markdown(empty_state_html("No audit log entries yet."), unsafe_allow_html=True)
    else:
        # get_audit_log() already orders most-recent-first (ORDER BY
        # created_at DESC) -- no re-sorting needed here.
        audit_df = pd.DataFrame(audit_rows)[["actor_email", "action", "target_type", "target_id", "details", "created_at"]]
        audit_df.columns = ["Actor", "Action", "Target Type", "Target ID", "Details", "Timestamp"]
        st.dataframe(audit_df, use_container_width=True, hide_index=True)
