
document.addEventListener("DOMContentLoaded", () => {
    const grid = document.getElementById("games-grid");
    const sentinel = document.getElementById("games-scroll-sentinel");
    const status = document.getElementById("games-loading-status");

    if (!grid || !sentinel || !status) {
        return;
    }

    let nextPage = 2;
    let isLoading = false;
    let hasMore = true;

    const observer = new IntersectionObserver(async (entries) => {
        if (!entries[0].isIntersecting || isLoading || !hasMore) {
            return;
        }

        isLoading = true;
        status.textContent = "Loading more games...";

        try {
            const url = new URL(window.location.pathname, window.location.origin);
            url.searchParams.set("page", nextPage);
            url.searchParams.set("load_more", "1");

            const response = await fetch(url, {
                headers: {
                    "X-Requested-With": "XMLHttpRequest"
                }
            });

            if (!response.ok) {
                throw new Error(`HTTP error: ${response.status}`);
            }

            const html = await response.text();
            const template = document.createElement("template");
            template.innerHTML = html.trim();

            const newCards = template.content.querySelectorAll(".game-card");

            if (newCards.length === 0) {
                hasMore = false;
                observer.disconnect();
                status.textContent = "All games loaded.";
                return;
            }

            grid.appendChild(template.content);
            nextPage += 1;

            if (newCards.length < 20) {
                hasMore = false;
                observer.disconnect();
                status.textContent = "All games loaded.";
            } else {
                status.textContent = "";
            }
        } catch (error) {
            console.error("Infinite Scroll error:", error);
            status.textContent = "Could not load more games.";
        } finally {
            isLoading = false;
        }
    }, {
        rootMargin: "300px"
    });

    observer.observe(sentinel);
});
