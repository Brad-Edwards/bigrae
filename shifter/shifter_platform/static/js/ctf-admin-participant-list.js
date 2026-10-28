/**
 * CTF admin participant-list page actions.
 *
 * Bulk "reset and send credentials" plus per-row "send link" (resend invite).
 *
 * Extracted from the inline <script> in
 * templates/ctf/admin/participant_list.html so the template stays within
 * SonarCloud's Web:LongJavaScriptCheck limit. The send-invitations URL and CSRF
 * token are read from the #participant-list-config JSON payload.
 */

let sendInvitationsUrl = '';
let csrfToken = '';

// Read the server's error text from the shared API envelope
// {"error": {"code", "message"}} or a legacy flat {"error": "<message>"}.
// Any other shape returns the fallback, so a non-string is never shown.
function apiErrorMessage(data, fallback) {
    const error = data?.error;
    if (typeof error === 'string' && error) {
        return error;
    }
    if (typeof error?.message === 'string' && error.message) {
        return error.message;
    }
    return fallback;
}

function postJson(url) {
    return fetch(url, {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken,
            'Content-Type': 'application/json',
        },
    }).then(function (response) { return response.json(); });
}

function sendAllInvites() {
    if (!confirm('Reset and send credentials to all participants with email?')) return;

    postJson(sendInvitationsUrl)
        .then(function (data) {
            if (data.success) {
                alert(`Sent ${data.sent} invitation(s). Failed: ${data.failed}.`);
                location.reload();
            } else {
                alert('Error: ' + apiErrorMessage(data, 'Failed to send invitations'));
            }
        })
        .catch(function () {
            // A network failure or a non-JSON response: never show the
            // browser exception text.
            alert('Error sending invitations. Please try again.');
        });
}

function resendInvite(participantId) {
    if (!confirm('Reset this participant password and send credentials?')) return;

    postJson(`/api/v1/ctf/participants/${participantId}/resend-invite/`)
        .then(function (data) {
            if (data.success) {
                alert('Invitation resent successfully.');
            } else {
                alert('Error: ' + apiErrorMessage(data, 'Failed to resend invite'));
            }
        })
        .catch(function () {
            alert('Error resending invite. Please try again.');
        });
}

document.addEventListener('DOMContentLoaded', function () {
    const configEl = document.getElementById('participant-list-config');
    if (configEl) {
        const config = JSON.parse(configEl.textContent);
        sendInvitationsUrl = config.sendInvitationsUrl;
        csrfToken = config.csrfToken;
    }

    document.querySelectorAll('.js-resend-invite').forEach(function (button) {
        button.addEventListener('click', function () {
            resendInvite(button.dataset.participantId);
        });
    });
});

globalThis.sendAllInvites = sendAllInvites;
