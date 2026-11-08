
document.addEventListener("DOMContentLoaded", function () {

    const form = document.getElementById("registerForm");
    const button = document.getElementById("registerButton");

    const generalError = document.getElementById("registerError");
    const successMessage = document.getElementById("registerSuccess");

    const username = document.getElementById("id_username");
    const password1 = document.getElementById("id_password1");
    const password2 = document.getElementById("id_password2");

    const fieldErrors = {
        username: document.getElementById("usernameError"),
        password1: document.getElementById("password1Error"),
        password2: document.getElementById("password2Error")
    };

    if (!form || !button) {
        return;
    }


    // SHOW MESSAGE
    function showMessage(element, message) {
        if (!element) return;

        element.textContent = message;
        element.hidden = false;
    }


    // HIDE MESSAGE
    function hideMessage(element) {
        if (!element) return;

        element.textContent = "";
        element.hidden = true;
    }


    // CLEAR ALL ERRORS
    function clearErrors() {

        hideMessage(generalError);
        hideMessage(successMessage);

        Object.values(fieldErrors).forEach(function (element) {
            hideMessage(element);
        });

    }


    // DISPLAY DJANGO VALIDATION ERRORS
    function showDjangoErrors(errors) {

        if (!errors) {
            showMessage(
                generalError,
                "Please check your information."
            );
            return;
        }

        Object.entries(errors).forEach(function ([field, messages]) {

            const text = messages
                .map(item => item.message)
                .join(" ");

            if (fieldErrors[field]) {
                showMessage(fieldErrors[field], text);
            } else {
                showMessage(generalError, text);
            }

        });

    }


    // REGISTER FORM SUBMIT
    form.addEventListener("submit", async function (event) {

        event.preventDefault();

        if (button.disabled) {
            return;
        }

        clearErrors();

        const usernameValue = username.value.trim();
        const passwordValue = password1.value;
        const confirmValue = password2.value;

        // CHECK EMPTY FIELDS
        if (!usernameValue || !passwordValue || !confirmValue) {
            showMessage(
                generalError,
                "Please fill in all fields."
            );
            return;
        }

        // CHECK PASSWORD MATCH
        if (passwordValue !== confirmValue) {
            showMessage(
                fieldErrors.password2,
                "Passwords do not match."
            );
            return;
        }

        button.disabled = true;
        button.textContent = "Creating account...";

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

            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }

            const result = await response.json();

            if (!response.ok || !result.success) {

                if (result.errors) {
                    showDjangoErrors(result.errors);
                } else {
                    showMessage(
                        generalError,
                        result.message || "Registration failed."
                    );
                }

                return;
            }

            // SUCCESS
            showMessage(
                successMessage,
                result.message || "Account created successfully!"
            );

            form.reset();

            button.textContent = "Account Created";

            // GO TO LOGIN PAGE
            window.location.assign(result.redirect_url);

        } catch (error) {

            console.error(error);

            showMessage(
                generalError,
                "Something went wrong. Please try again."
            );

        } finally {

            if (!successMessage || successMessage.hidden) {
                button.disabled = false;
                button.textContent = "Create Account";
            }

        }

    });

});
