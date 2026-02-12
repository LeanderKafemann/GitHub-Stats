#!/usr/bin/python3

import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple, Any, cast

import aiohttp
import requests


###############################################################################
# Main Classes
###############################################################################


class Queries(object):
    """
    Class with functions to query the GitHub GraphQL (v4) API and the REST (v3)
    API. Also includes functions to dynamically generate GraphQL queries.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        max_connections: int = 10,
    ):
        self.username = username
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def query(self, generated_query: str) -> Dict:
        """
        Make a request to the GraphQL API using the authentication token from
        the environment
        :param generated_query: string query to be sent to the API
        :return: decoded GraphQL JSON output
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }
        try:
            async with self.semaphore:
                r_async = await self.session.post(
                    "https://api.github.com/graphql",
                    headers=headers,
                    json={"query": generated_query},
                )
            result = await r_async.json()
            if result is not None:
                return result
        except:
            print("aiohttp failed for GraphQL query")
            # Fall back on non-async requests
            async with self.semaphore:
                r_requests = requests.post(
                    "https://api.github.com/graphql",
                    headers=headers,
                    json={"query": generated_query},
                )
                result = r_requests.json()
                if result is not None:
                    return result
        return dict()

    async def query_rest(
        self,
        path: str,
        params: Optional[Dict] = None,
        max_retries: int = 10,
    ) -> Any:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :param max_retries: Maximum number of 202 retries before giving up
        :return: deserialized REST JSON output (may be a dict or a list)
        """
        # Normalize path once, not inside the loop
        if path.startswith("/"):
            path = path[1:]
        if params is None:
            params = dict()

        headers = {
            "Authorization": f"token {self.access_token}",
        }
        url = f"https://api.github.com/{path}"
        params_tuple = tuple(params.items())

        for attempt in range(max_retries):
            try:
                async with self.semaphore:
                    r_async = await self.session.get(
                        url,
                        headers=headers,
                        params=params_tuple,
                    )
                if r_async.status == 202:
                    # GitHub is computing stats – back off and retry with exponential backoff
                    if attempt < max_retries - 1:
                        wait_time = 5 * (2 ** attempt)  # Exponential backoff: 5s, 10s, 20s, 40s...
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        print(f"  [REST] Gave up after {max_retries} retries "
                              f"(202) for {path}")
                        return dict()
                if r_async.status == 204:
                    return dict()
                if r_async.status == 403:
                    return dict()
                if r_async.status != 200:
                    print(f"  [REST] {path} returned {r_async.status}")
                    return dict()

                result = await r_async.json()
                if result is not None:
                    return result
            except Exception as e:
                print(f"aiohttp failed for {path}: {e}")
                # Fall back on non-async requests
                try:
                    async with self.semaphore:
                        r_requests = requests.get(
                            url,
                            headers=headers,
                            params=params_tuple,
                        )
                    if r_requests.status_code == 202:
                        if attempt < max_retries - 1:
                            wait_time = 5 * (2 ** attempt)  # Exponential backoff
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            return dict()
                    elif r_requests.status_code == 403:
                        return dict()
                    elif r_requests.status_code == 200:
                        return r_requests.json()
                except Exception as e2:
                    print(f"requests also failed for {path}: {e2}")

        return dict()

    @staticmethod
    def repos_overview(
        contrib_cursor: Optional[str] = None, owned_cursor: Optional[str] = None
    ) -> str:
        """
        :return: GraphQL query with overview of user repositories
        """
        return f"""{{
  viewer {{
    login,
    name,
    repositories(
        first: 100,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        isFork: false,
        after: {"null" if owned_cursor is None else '"'+ owned_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
    repositoriesContributedTo(
        first: 100,
        includeUserRepositories: false,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        contributionTypes: [
            COMMIT,
            PULL_REQUEST,
            REPOSITORY,
            PULL_REQUEST_REVIEW
        ]
        after: {"null" if contrib_cursor is None else '"'+ contrib_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    @staticmethod
    def contrib_years() -> str:
        """
        :return: GraphQL query to get all years the user has been a contributor
        """
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        """
        :param year: year to query for
        :return: portion of a GraphQL query with desired info for a given year
        """
        return f"""
    year{year}: contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{int(year) + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: List[str]) -> str:
        """
        :param years: list of years to get contributions for
        :return: query to retrieve contribution information for all user years
        """
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Stats(object):
    """
    Retrieve and store statistics about GitHub usage.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        exclude_repos: Optional[Set] = None,
        exclude_langs: Optional[Set] = None,
        ignore_forked_repos: bool = False,
    ):
        self.username = username
        self._ignore_forked_repos = ignore_forked_repos
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self.queries = Queries(username, access_token, session)

        self._name: Optional[str] = None
        self._stargazers: Optional[int] = None
        self._forks: Optional[int] = None
        self._total_contributions: Optional[int] = None
        self._languages: Optional[Dict[str, Any]] = None
        self._repos: Optional[Set[str]] = None
        self._lines_changed: Optional[Tuple[int, int]] = None
        self._views: Optional[int] = None
        self._lines_changed_by_week: Optional[Dict[str, Tuple[int, int]]] = None
        self._contributions_by_year: Optional[Dict[int, int]] = None
        self._login: Optional[str] = None

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        lines_changed = await self.lines_changed
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.repos)}
Lines of code added: {lines_changed[0]:,}
Lines of code deleted: {lines_changed[1]:,}
Lines of code changed: {lines_changed[0] + lines_changed[1]:,}
Project page views: {await self.views:,}
Languages:
  - {formatted_languages}"""

    async def build_snapshot(self) -> Dict[str, Any]:
        """
        Build a snapshot of current statistics for persistence in history.json.
        """
        languages = await self.languages
        lang_snapshot = {}
        for name, data in languages.items():
            lang_snapshot[name] = {
                "size": data.get("size", 0),
                "prop": data.get("prop", 0.0),
                "color": data.get("color"),
            }

        lines = await self.lines_changed
        contribs_by_year = await self.contributions_by_year
        weekly = await self.lines_changed_by_week
        weekly_serializable = {k: list(v) for k, v in weekly.items()}

        total_contribs = sum(contribs_by_year.values())

        return {
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "stargazers": await self.stargazers,
            "forks": await self.forks,
            "total_contributions": total_contribs,
            "repo_count": len(await self.repos),
            "lines_added": lines[0],
            "lines_deleted": lines[1],
            "languages": lang_snapshot,
            "contributions_by_year": {
                str(k): v for k, v in contribs_by_year.items()
            },
            "lines_changed_by_week": weekly_serializable,
        }

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes
        """
        self._stargazers = 0
        self._forks = 0
        self._languages = dict()
        self._repos = set()

        exclude_langs_lower = {x.lower() for x in self._exclude_langs}

        next_owned = None
        next_contrib = None
        while True:
            raw_results = await self.queries.query(
                Queries.repos_overview(
                    owned_cursor=next_owned, contrib_cursor=next_contrib
                )
            )
            raw_results = raw_results if raw_results is not None else {}

            viewer = raw_results.get("data", {}).get("viewer", {})

            if self._login is None:
                self._login = viewer.get("login")

            self._name = viewer.get("name", None)
            if self._name is None:
                self._name = viewer.get("login", "No Name")

            contrib_repos = viewer.get("repositoriesContributedTo", {})
            owned_repos = viewer.get("repositories", {})

            repos = owned_repos.get("nodes", [])
            if not self._ignore_forked_repos:
                repos += contrib_repos.get("nodes", [])

            for repo in repos:
                if repo is None:
                    continue
                name = repo.get("nameWithOwner")
                if name in self._repos or name in self._exclude_repos:
                    continue
                self._repos.add(name)
                self._stargazers += repo.get("stargazers").get("totalCount", 0)
                self._forks += repo.get("forkCount", 0)

                for lang in repo.get("languages", {}).get("edges", []):
                    name = lang.get("node", {}).get("name", "Other")
                    languages = await self.languages
                    if name.lower() in exclude_langs_lower:
                        continue
                    if name in languages:
                        languages[name]["size"] += lang.get("size", 0)
                        languages[name]["occurrences"] += 1
                    else:
                        languages[name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color"),
                        }

            if owned_repos.get("pageInfo", {}).get(
                "hasNextPage", False
            ) or contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = owned_repos.get("pageInfo", {}).get(
                    "endCursor", next_owned
                )
                next_contrib = contrib_repos.get("pageInfo", {}).get(
                    "endCursor", next_contrib
                )
            else:
                break

        langs_total = sum([v.get("size", 0) for v in self._languages.values()])
        for k, v in self._languages.items():
            v["prop"] = 100 * (v.get("size", 0) / langs_total)

    async def _get_login(self) -> str:
        """
        Return the exact GitHub login for use in REST API comparisons.
        """
        if self._login is not None:
            return self._login
        await self.get_stats()
        return self._login if self._login is not None else self.username

    async def _fetch_contributor_stats(self) -> None:
        """
        Fetch weekly contributor stats for all repos in parallel and populate
        both _lines_changed and _lines_changed_by_week from the same data.
        """
        login = (await self._get_login()).lower()
        repos = list(await self.repos)
        print(f"Fetching contributor stats for {len(repos)} repos...")

        async def fetch_one(repo: str) -> Tuple[int, int, Dict[str, List[int]]]:
            """Fetch stats for a single repo, return (adds, dels, weekly)."""
            adds = 0
            dels = 0
            weekly: Dict[str, List[int]] = {}

            r = await self.queries.query_rest(
                f"/repos/{repo}/stats/contributors", max_retries=10
            )
            if not isinstance(r, list):
                return adds, dels, weekly

            for author_obj in r:
                if not isinstance(author_obj, dict):
                    continue
                author_info = author_obj.get("author")
                if not isinstance(author_info, dict):
                    continue
                if author_info.get("login", "").lower() != login:
                    continue

                for week in author_obj.get("weeks", []):
                    a = week.get("a", 0)
                    d = week.get("d", 0)
                    adds += a
                    dels += d

                    if a == 0 and d == 0:
                        continue
                    timestamp = week.get("w", 0)
                    date_str = datetime.utcfromtimestamp(timestamp).strftime(
                        "%Y-%m-%d"
                    )
                    if date_str not in weekly:
                        weekly[date_str] = [0, 0]
                    weekly[date_str][0] += a
                    weekly[date_str][1] += d

            return adds, dels, weekly

        # Run all repo fetches concurrently (bounded by the semaphore)
        results = await asyncio.gather(*[fetch_one(repo) for repo in repos])

        total_adds = 0
        total_dels = 0
        merged_weekly: Dict[str, List[int]] = {}
        for adds, dels, weekly in results:
            total_adds += adds
            total_dels += dels
            for date_str, vals in weekly.items():
                if date_str not in merged_weekly:
                    merged_weekly[date_str] = [0, 0]
                merged_weekly[date_str][0] += vals[0]
                merged_weekly[date_str][1] += vals[1]

        self._lines_changed = (total_adds, total_dels)
        self._lines_changed_by_week = {
            k: (v[0], v[1]) for k, v in sorted(merged_weekly.items())
        }
        print(f"  Lines changed: +{total_adds:,} -{total_dels:,}")

    @property
    async def name(self) -> str:
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert self._name is not None
        return self._name

    @property
    async def stargazers(self) -> int:
        if self._stargazers is not None:
            return self._stargazers
        await self.get_stats()
        assert self._stargazers is not None
        return self._stargazers

    @property
    async def forks(self) -> int:
        if self._forks is not None:
            return self._forks
        await self.get_stats()
        assert self._forks is not None
        return self._forks

    @property
    async def languages(self) -> Dict:
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert self._languages is not None
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        if self._languages is None:
            await self.get_stats()
            assert self._languages is not None
        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def repos(self) -> Set[str]:
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert self._repos is not None
        return self._repos

    @property
    async def total_contributions(self) -> int:
        if self._total_contributions is not None:
            return self._total_contributions

        self._total_contributions = 0
        years = (
            (await self.queries.query(Queries.contrib_years()))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionYears", [])
        )
        by_year = (
            (await self.queries.query(Queries.all_contribs(years)))
            .get("data", {})
            .get("viewer", {})
            .values()
        )
        for year in by_year:
            self._total_contributions += year.get("contributionCalendar", {}).get(
                "totalContributions", 0
            )
        return cast(int, self._total_contributions)

    @property
    async def contributions_by_year(self) -> Dict[int, int]:
        if self._contributions_by_year is not None:
            return self._contributions_by_year

        self._contributions_by_year = {}
        years = (
            (await self.queries.query(Queries.contrib_years()))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionYears", [])
        )
        raw = (
            (await self.queries.query(Queries.all_contribs(years)))
            .get("data", {})
            .get("viewer", {})
        )
        for key, value in raw.items():
            if key.startswith("year"):
                year_int = int(key[4:])
                total = value.get("contributionCalendar", {}).get(
                    "totalContributions", 0
                )
                self._contributions_by_year[year_int] = total

        return self._contributions_by_year

    @property
    async def lines_changed(self) -> Tuple[int, int]:
        if self._lines_changed is not None:
            return self._lines_changed
        await self._fetch_contributor_stats()
        assert self._lines_changed is not None
        return self._lines_changed

    @property
    async def lines_changed_by_week(self) -> Dict[str, Tuple[int, int]]:
        if self._lines_changed_by_week is not None:
            return self._lines_changed_by_week
        await self._fetch_contributor_stats()
        assert self._lines_changed_by_week is not None
        return self._lines_changed_by_week

    @property
    async def views(self) -> int:
        """
        Note: only returns views for the last 14 days (as-per GitHub API)
        :return: total number of page views the user's projects have received
        """
        if self._views is not None:
            return self._views

        repos = list(await self.repos)

        async def fetch_views(repo: str) -> int:
            r = await self.queries.query_rest(
                f"/repos/{repo}/traffic/views", max_retries=5
            )
            if isinstance(r, dict):
                return r.get("count", 0)
            return 0

        results = await asyncio.gather(*[fetch_views(repo) for repo in repos])
        self._views = sum(results)
        return self._views


###############################################################################
# Main Function
###############################################################################


async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    if access_token is None or user is None:
        raise RuntimeError(
            "ACCESS_TOKEN and GITHUB_ACTOR environment variables cannot be None!"
        )
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())


if __name__ == "__main__":
    asyncio.run(main())