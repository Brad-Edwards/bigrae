/* global initScoreTimeline */
// Admin participant-detail page actions: resend invite, disqualify.
// Extracted from the inline `<script>` block in
// templates/ctf/admin/participant_detail.html so the template stays
// within Sonar Web:LongJavaScriptCheck limits.

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

function resendInvite(participantId, csrfToken) {
    if (!globalThis.confirm('Resend invitation email to this participant?')) return;

    return globalThis.fetch('/api/v1/ctf/participants/' + participantId + '/resend-invite/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken,
            'Content-Type': 'application/json'
        }
    })
    .then(function(response) { return response.json(); })
    .then(function(data) {
        if (data.success) {
            globalThis.alert('Invitation resent successfully.');
            globalThis.location.reload();
        } else {
            globalThis.alert('Error: ' + apiErrorMessage(data, 'Failed to resend invite'));
        }
    })
    .catch(function() {
        // A network failure or a non-JSON response: never show the browser
        // exception text.
        globalThis.alert('Error resending invite. Please try again.');
    });
}

function disqualifyParticipant(_participantId) {
    if (!globalThis.confirm('Are you sure you want to disqualify this participant? This action cannot be undone.')) return;
    // Disqualify API endpoint not yet implemented.
    globalThis.alert('Disqualify functionality not yet implemented via API.');
}

function initParticipantTimeline(participantId) {
    initScoreTimeline('score-timeline-chart', '/api/v1/ctf/participants/' + participantId + '/score-timeline/');
}

globalThis.resendInvite = resendInvite;
globalThis.disqualifyParticipant = disqualifyParticipant;
globalThis.initParticipantTimeline = initParticipantTimeline;
