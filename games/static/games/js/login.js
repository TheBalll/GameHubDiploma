
document.addEventListener("DOMContentLoaded", function () {

    const form = document.getElementById("loginForm");
    const button = document.getElementById("loginButton");
    const errorMessage = document.getElementById("loginError");

    if (!form || !button || !errorMessage) return;

    form.addEventListener("submit", async function (event) {
        event.preventDefault();

        if (button.disabled) return;

        errorMessage.hidden = true;
        errorMessage.textContent = "";

        const username = form.querySelector("#username").value.trim();
        const password = form.querySelector("#password").value;

        if (!username || !password) {
            errorMessage.textContent =
                "Please enter your username and password.";
            errorMessage.hidden = false;
            return;
        }

        button.disabled = true;
        button.textContent = "Logging in...";

        try {
            const response = await fetch(form.action, {
                method: "POST",
                credentials: "same-origin",
                body: new FormData(form),
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json"
                }
            });

            const result = await response.json();

            if (!response.ok || !result.success) {
                throw new Error(
                    result.message || "Unable to log in."
                );
            }

            window.location.assign(result.redirect_url);

        } catch (error) {
            errorMessage.textContent =
                error.message || "Something went wrong.";
            errorMessage.hidden = false;

            button.disabled = false;
            button.textContent = "Login";
        }
    });

});
