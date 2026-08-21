const API_URL = (window.SENTINELPAY_API_BASE_URL || window.location.origin).replace(/\/$/, "");

const authPage = document.getElementById("authPage");
const dashboardMain = document.getElementById("dashboardMain");
const appHeader = document.getElementById("appHeader");
const loginForm = document.getElementById("loginForm");
const registerForm = document.getElementById("registerForm");
const authMessage = document.getElementById("authMessage");
const authenticatedUser = document.getElementById("authenticatedUser");
const logoutButton = document.getElementById("logoutButton");
const form = document.getElementById("transactionForm");
const checkButton = document.getElementById("checkButton");
const historyBody = document.getElementById("historyBody");
const riskScore = document.getElementById("riskScore");
const riskLevel = document.getElementById("riskLevel");
const decision = document.getElementById("decision");
const explanationText = document.getElementById("riskExplanation");
const riskBar = document.getElementById("riskBar");
const transactionCount = document.getElementById("transactionCount");
const highRiskCount = document.getElementById("highRiskCount");
const allowedCount = document.getElementById("allowedCount");
const blockedCount = document.getElementById("blockedCount");

let stats = { total: 0, high: 0, allowed: 0, blocked: 0 };
let isSubmitting = false;
const defaultButtonContent = checkButton.innerHTML;

function setAuthMessage(message = "", success = false) {
    authMessage.textContent = message;
    authMessage.classList.toggle("hidden", !message);
    authMessage.classList.toggle("success", Boolean(message && success));
}

function showAuthForm(formName = "login") {
    const login = formName === "login";
    loginForm.classList.toggle("hidden", !login);
    registerForm.classList.toggle("hidden", login);
    document.getElementById("authTitle").textContent = login
        ? "Sign in to your workspace"
        : "Create your secure workspace";
    setAuthMessage();
}

function showLogin(message = "") {
    dashboardMain.classList.add("hidden");
    appHeader.classList.add("hidden");
    authPage.classList.remove("hidden");
    showAuthForm("login");
    if (message) setAuthMessage(message);
}

function showDashboard(user) {
    authenticatedUser.textContent = user.email;
    authPage.classList.add("hidden");
    appHeader.classList.remove("hidden");
    dashboardMain.classList.remove("hidden");
    loadTransactionHistory();
}

async function apiFetch(path, options = {}) {
    const response = await fetch(`${API_URL}${path}`, { ...options, credentials: "include" });
    if (response.status === 401 && path !== "/auth/me") {
        showLogin("Your session has expired. Please sign in again.");
    }
    return response;
}

async function responseMessage(response, fallback) {
    try {
        const data = await response.json();
        return typeof data.detail === "string" ? data.detail : fallback;
    } catch {
        return fallback;
    }
}

function setButtonLoading(button, loading, label = "") {
    if (!button.dataset.defaultLabel) button.dataset.defaultLabel = button.textContent;
    button.disabled = loading;
    button.setAttribute("aria-busy", String(loading));
    button.textContent = loading ? label : button.dataset.defaultLabel;
}

async function submitCredentials(event, endpoint, button) {
    event.preventDefault();
    const currentForm = event.currentTarget;
    if (!currentForm.reportValidity()) return;
    const email = currentForm.querySelector('input[type="email"]').value.trim();
    const password = currentForm.querySelector('input[type="password"]').value;
    setAuthMessage();
    setButtonLoading(button, true, endpoint === "/auth/login" ? "SIGNING IN..." : "CREATING ACCOUNT...");
    try {
        const response = await apiFetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password }),
        });
        if (!response.ok) throw new Error(await responseMessage(response, "Unable to complete authentication. Please try again."));
        const data = await response.json();
        if (!data.user?.email) throw new Error("Authentication response was invalid.");
        currentForm.reset();
        showDashboard(data.user);
    } catch (error) {
        setAuthMessage(error instanceof TypeError
            ? "Unable to reach SentinelPay. Please check your connection and try again."
            : error.message);
    } finally {
        setButtonLoading(button, false);
    }
}

loginForm.addEventListener("submit", (event) => submitCredentials(event, "/auth/login", document.getElementById("loginButton")));
registerForm.addEventListener("submit", (event) => submitCredentials(event, "/auth/register", document.getElementById("registerButton")));
document.getElementById("showRegister").addEventListener("click", () => showAuthForm("register"));
document.getElementById("showLogin").addEventListener("click", () => showAuthForm("login"));

logoutButton.addEventListener("click", async () => {
    setButtonLoading(logoutButton, true, "LOGGING OUT...");
    try {
        const response = await apiFetch("/auth/logout", { method: "POST" });
        if (!response.ok) throw new Error(await responseMessage(response, "Unable to log out. Please try again."));
        showLogin();
    } catch (error) {
        setAuthMessage(error instanceof TypeError ? "Unable to reach SentinelPay. Please try again." : error.message);
    } finally {
        setButtonLoading(logoutButton, false);
    }
});

async function submitTransaction(event) {
    event.preventDefault();
    if (isSubmitting || !form.reportValidity()) return;
    isSubmitting = true;
    checkButton.disabled = true;
    checkButton.setAttribute("aria-busy", "true");
    checkButton.innerHTML = "⏳ ANALYZING TRANSACTION...";
    const transaction = {
        amount: Number(document.getElementById("amount").value),
        sender: document.getElementById("sender").value,
        receiver: document.getElementById("receiver").value,
        location: document.getElementById("location").value,
        device: document.getElementById("device").value,
        velocity: Number(document.getElementById("velocity").value),
    };
    try {
        const response = await apiFetch("/transaction/check", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(transaction),
        });
        if (!response.ok) throw new Error(await responseMessage(response, `Backend returned ${response.status}`));
        const data = await response.json();
        if (!data.transaction || !Number.isFinite(data.risk_score)) throw new Error("Backend returned an invalid assessment response");
        showResult(data);
        updateStats(data);
        await loadTransactionHistory();
    } catch (error) {
        if (error instanceof TypeError) alert("Transaction analysis could not reach SentinelPay. Please try again.");
        else if (!dashboardMain.classList.contains("hidden")) alert(`Transaction analysis failed.\n\n${error.message}`);
    } finally {
        isSubmitting = false;
        checkButton.disabled = false;
        checkButton.removeAttribute("aria-busy");
        checkButton.innerHTML = defaultButtonContent;
    }
}

form.addEventListener("submit", submitTransaction);

function formatTransactionDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unavailable" : date.toLocaleString();
}

function setHistoryState(message, className = "no-data") {
    historyBody.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.className = className;
    cell.textContent = message;
    row.appendChild(cell);
    historyBody.appendChild(row);
}

function renderTransactionHistory(transactions) {
    if (!transactions.length) return setHistoryState("No transactions analyzed yet.");
    historyBody.replaceChildren();
    transactions.forEach((transaction) => {
        const row = document.createElement("tr");
        [formatTransactionDate(transaction.created_at), `₹${Number(transaction.amount).toLocaleString("en-IN")}`,
            transaction.sender, transaction.receiver, `${transaction.risk_score}/100`, transaction.risk_level,
            transaction.decision, transaction.analysis_source].forEach((value) => {
            const cell = document.createElement("td");
            cell.textContent = value;
            row.appendChild(cell);
        });
        historyBody.appendChild(row);
    });
}

async function loadTransactionHistory() {
    setHistoryState("Loading transaction history…");
    try {
        const response = await apiFetch("/transactions");
        if (!response.ok) throw new Error(await responseMessage(response, "Unable to load transaction history."));
        const transactions = await response.json();
        if (!Array.isArray(transactions)) throw new Error("History response was invalid.");
        renderTransactionHistory(transactions);
    } catch {
        if (!dashboardMain.classList.contains("hidden")) setHistoryState("Unable to load transaction history. Please refresh and try again.");
    }
}

function showResult(data) {
    document.getElementById("riskTitle").textContent = "Transaction Analyzed";
    document.getElementById("riskSubtitle").textContent = data.analysis_source === "gemini"
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

function getRiskColor(level) {
    return { LOW: "#52e39a", MEDIUM: "#f6c453", HIGH: "#ff6b6b" }[level] || "#6f8cff";
}

async function initializeAuthentication() {
    try {
        const response = await apiFetch("/auth/me");
        if (response.ok) {
            const data = await response.json();
            if (data.user?.email) return showDashboard(data.user);
        }
    } catch {
        // The sign-in page provides the safe retry path for network failures.
    }
    showLogin();
}

initializeAuthentication();
