document.addEventListener('DOMContentLoaded', () => {
    const hrDisplay = document.getElementById('hr-display');

    function resetHeartRateDisplay() {
        hrDisplay.textContent = '--';
        hrDisplay.style.display = 'none';
    }

    resetHeartRateDisplay();

    const ws = new WebSocket('ws://localhost:8765');

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'heart_rate') {
            hrDisplay.textContent = data.bpm;
            hrDisplay.style.display = 'block';
        }
    };

    ws.onclose = () => {
        resetHeartRateDisplay();
    };
});