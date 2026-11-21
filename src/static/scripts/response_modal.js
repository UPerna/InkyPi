let responseTimer;
let trackedRefresh;

// Function to Show the Response Modal
function showResponseModal(status, message) {
    const modal = document.getElementById('responseModal');
    const modalContent = document.getElementById('modalContent');
    const modalMessage = document.getElementById('modalMessage');

    // Remove any previous status classes
    modal.classList.remove('success', 'failure');

    // Add the correct class based on the status
    if (status === 'success') {
        modal.classList.add('success'); // Apply success class
    } else {
        modal.classList.add('failure'); // Apply failure class
    }
    modalMessage.textContent = message;

    // Display Modal
    modal.style.display = 'block';

    // Auto-Close Modal After 10 Seconds
    clearTimeout(responseTimer);
    responseTimer = setTimeout(closeResponseModal, 10000);
}

// Function to Close the Modal
function closeResponseModal() {
    const modal = document.getElementById('responseModal');
    modal.style.display = 'none';
}

async function trackRefresh(job, onCompleted) {
    trackedRefresh = job.id;
    showResponseModal('pending', job.message);
    try {
        while (trackedRefresh === job.id) {
            const response = await fetch(job.status_url, {cache: 'no-store'});
            if (trackedRefresh !== job.id) return;
            if (!response.ok) throw new Error();
            const result = await response.json();
            if (trackedRefresh !== job.id) return;
            if (result.state !== 'queued' && result.state !== 'running') {
                if (result.state === 'completed' && onCompleted) {
                    onCompleted(result);
                    return;
                }
                showResponseModal(result.state === 'failed' ? 'failure' : result.state === 'completed' ? 'success' : 'pending',
                                  result.message);
                return;
            }
            await new Promise(resolve => setTimeout(resolve, 1000));
        }
    } catch {
        if (trackedRefresh === job.id) showResponseModal('failure', 'Refresh status is unavailable.');
    }
}
