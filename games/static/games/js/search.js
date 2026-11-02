console.log('GameHub search.js loaded');
const searchInput = document.getElementById('game-search');
const searchResults = document.getElementById('search-results');

if (searchInput && searchResults) {
    searchInput.addEventListener('input', async function () {
        const query = searchInput.value.trim();

        if (query.length === 0) {
            searchResults.innerHTML = '';
            searchResults.classList.remove('active');
            return;
        }

        const response = await fetch(
            `/search-games/?q=${encodeURIComponent(query)}`
        );

        const data = await response.json();

        searchResults.innerHTML = '';

        if (data.games.length === 0) {
            searchResults.innerHTML =
                '<div class="search-no-results">No games found</div>';

            searchResults.classList.add('active');
            return;
        }

        data.games.forEach(function (game) {
            const link = document.createElement('a');
            link.href = `/games/${game.id}/`;
            link.className = 'search-result-item';

            const title = document.createElement('span');
            title.className = 'search-result-title';
            title.textContent = game.title;

            const info = document.createElement('span');
            info.className = 'search-result-info';
            info.textContent = `${game.genre} • ${game.release_year}`;

            link.appendChild(title);
            link.appendChild(info);

            searchResults.appendChild(link);
        });

        searchResults.classList.add('active');
    });
}