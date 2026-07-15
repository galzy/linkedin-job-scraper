from datetime import date

from linkedin_scraper.net.headers import (
    _ANCHOR_CHROME,
    _build_profiles,
    current_chrome_major,
)


def test_projects_forward_over_time_and_stays_monotonic():
    one_year = current_chrome_major(date(2027, 7, 1))
    two_years = current_chrome_major(date(2028, 7, 1))
    assert _ANCHOR_CHROME < one_year < two_years


def test_past_dates_clamp_to_the_baseline():
    assert current_chrome_major(date(2020, 1, 1)) == _ANCHOR_CHROME


def test_built_profiles_keep_each_ua_matched_to_its_client_hints():
    profiles = _build_profiles(151, 27)
    chrome_majors = set()
    for profile in profiles:
        if "Sec-CH-UA" in profile:  # a Chrome entry
            major = profile["User-Agent"].split("Chrome/")[1].split(".")[0]
            assert f'v="{major}"' in profile["Sec-CH-UA"]  # client hints match this entry's UA
            chrome_majors.add(major)
    assert chrome_majors == {"151", "150"}  # three latest plus one a version back
    assert any("Version/27.0" in p["User-Agent"] for p in profiles)  # the Safari entry
