console.log("SentinelPay script loaded");

const configuredApiBaseUrl = document
    .querySelector('meta[name="sentinelpay-api-base-url"]')
    ?.content.trim();
const API_URL = (configuredApiBaseUrl || window.location.origin).replace(/\/$/, "");

const form = document.getElementById("transactionForm");

const checkButton = document.getElementById("checkButton");

const riskScore =
    document.getElementById("riskScore");

const riskLevel =
    document.getElementById("riskLevel");

const decision =
    document.getElementById("decision");

const explanationText =
    document.getElementById("riskExplanation");

const riskBar =
    document.getElementById("riskBar");

const historyBody =
    document.getElementById("historyBody");

const transactionCount =
    document.getElementById("transactionCount");

const highRiskCount =
    document.getElementById("highRiskCount");

const allowedCount =
    document.getElementById("allowedCount");

const blockedCount =
    document.getElementById("blockedCount");


let stats = {
    total: 0,
    high: 0,
    allowed: 0,
    blocked: 0
};

let isSubmitting = false;
const defaultButtonContent = checkButton.innerHTML;


async function submitTransaction(event) {
    event.preventDefault();

    if (isSubmitting || !form.reportValidity()) {
        return;
    }

    isSubmitting = true;
    checkButton.disabled = true;
    checkButton.setAttribute("aria-busy", "true");

    checkButton.innerHTML = "⏳ ANALYZING TRANSACTION...";


    const transaction = {

        amount: Number(
            document.getElementById("amount").value
        ),

        sender:
            document.getElementById("sender").value,

        receiver:
            document.getElementById("receiver").value,

        location:
            document.getElementById("location").value,

        device:
            document.getElementById("device").value,

        velocity: Number(
            document.getElementById("velocity").value
        )
    };


    try {

        const response = await fetch(
            `${API_URL}/transaction/check`,
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify(transaction)
            }
        );
        if (!response.ok) {
            throw new Error(`Backend returned ${response.status}`);
        }

        const data = await response.json();

        if (!data.transaction || !Number.isFinite(data.risk_score)) {
            throw new Error("Backend returned an invalid assessment response");
        }

        showResult(data);
        updateStats(data);
        addHistory(data);

    } catch (error) {

        console.error(error);

       alert(
    "Transaction analysis failed.\n\n" +
    error.message
);

    } finally {
        isSubmitting = false;
        checkButton.disabled = false;
        checkButton.removeAttribute("aria-busy");
        checkButton.innerHTML = defaultButtonContent;
    }

}

form.addEventListener("submit", submitTransaction);

function showResult(data) {
    const riskTitleEl = document.getElementById("riskTitle");
    const riskSubtitleEl = document.getElementById("riskSubtitle");

    riskTitleEl.textContent = "Transaction Analyzed";
    riskSubtitleEl.textContent = data.analysis_source === "gemini"
        ? "Gemini AI-powered fraud risk assessment completed."
        : "Rule-based fallback assessment completed (Gemini unavailable).";
    riskScore.textContent = data.risk_score;
    riskLevel.textContent = data.risk_level;
    decision.textContent = data.decision;
    explanationText.textContent = data.explanation;

    riskBar.style.width = `${data.risk_score}%`;
    riskBar.style.backgroundColor = getRiskColor(data.risk_level);
    riskLevel.style.color = getRiskColor(data.risk_level);
    decision.style.color = getRiskColor(data.risk_level);
}

function updateStats(data) {
    stats.total += 1;
    if (data.risk_level === "HIGH") stats.high += 1;
    if (data.decision === "ALLOW") stats.allowed += 1;
    if (data.decision === "BLOCK") stats.blocked += 1;

    transactionCount.textContent = stats.total;
    highRiskCount.textContent = stats.high;
    allowedCount.textContent = stats.allowed;
    blockedCount.textContent = stats.blocked;
}

function addHistory(data) {
    const placeholder = historyBody.querySelector(".no-data");
    if (placeholder) placeholder.closest("tr").remove();

    const row = document.createElement("tr");
    const values = [
        data.transaction.sender,
        `₹${Number(data.transaction.amount).toLocaleString("en-IN")}`,
        data.risk_level,
        data.decision
    ];

    values.forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
    });

    historyBody.prepend(row);
}

function getRiskColor(level) {
    return { LOW: "#52e39a", MEDIUM: "#f6c453", HIGH: "#ff6b6b" }[level] || "#6f8cff";
}
