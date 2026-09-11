
"""
================================================================================
WAMEX Geochemistry Data Downloader (Downhole & Surface)
Website: https://wamexgeochem.net.au/
================================================================================

A completely generic, interactive, and reusable Python tool to query and download
exploration geochemistry data from the Geological Survey of Western Australia
(GSWA) WAMEX portal.

Features:
- No hardcoded locations, analytes, companies, or filter values.
- Dynamically reads available mapsheet/area options directly from WAMEX forms.
- Interactive runtime prompt wizard if run without arguments.
- Also supports non-interactive execution via command-line arguments.
- Leaving any prompt blank applies no filter (queries all records).
- Option '-1' available to request all matching records without a cap.
- Supports both Downhole and Surface geochemistry datasets.
- Spatial boundaries: Bounding box coordinates, Boundary 1 & Boundary 2
  corner points, or GeoJSON polygon file upload.
- Automated CSRF session handling, server query queuing, status polling,
  ZIP package download, and CSV table extraction.
- Automatic avoidance of duplicate downloads when existing files match.
- Segregated output directories for Downhole and Surface datasets.
- Correct detection of genuine zero-result responses vs JavaScript template strings.
"""
import os
import io
import re
import csv
import sys
import time
import json
import difflib
import zipfile
import argparse
import tempfile
from typing import Union, List, Tuple, Dict, Optional, Any
import requests

BASE_URL = "https://wamexgeochem.net.au"
# Valid limit values accepted by WAMEX backend (100000 is also valid per the form HTML)
VALID_LIMITS = [100, 500, 1000, 10000, 100000, -1]


class InvalidFilterError(ValueError):
    """Raised when a user-supplied filter value does not match any option
    dynamically loaded from the WAMEX server.

    This distinguishes an INVALID FILTER (typo / wrong casing / unknown value)
    from a GENUINE zero-result query, so a valid mapsheet is never falsely
    reported as 'No records found'.
    """

    def __init__(self, message: str, suggestions: Optional[List[str]] = None):
        super().__init__(message)
        self.suggestions = suggestions or []



class WamexDownloader:
    """
    Generic client for querying, packaging, and downloading harmonised
    geochemistry datasets from GSWA WAMEX.
    """

    def __init__(self, user_agent: Optional[str] = None):
        self.session = requests.Session()
        ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
        self.session.headers.update({"User-Agent": ua})

    # ------------------------------------------------------------------
    # Network helpers with retries and honest error reporting
    # ------------------------------------------------------------------
    def _request_with_retry(
        self,
        method: str,
        url: str,
        attempts: int = 3,
        timeout: int = 60,
        **kwargs: Any,
    ) -> requests.Response:
        """Perform an HTTP request with retries on transient network errors.

        Retries on ConnectionError / ReadTimeout with exponential backoff.
        Never swallows the final error: the last exception is re-raised with
        context about the URL involved.
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                if method.upper() == "GET":
                    return self.session.get(url, timeout=timeout, **kwargs)
                return self.session.post(url, timeout=timeout, **kwargs)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt < attempts:
                    wait = 2 * attempt
                    print(f"  [!] Network error on {method} {url} "
                          f"(attempt {attempt}/{attempts}): {exc}. "
                          f"Retrying in {wait}s...")
                    time.sleep(wait)
        raise RuntimeError(
            f"Network failure after {attempts} attempts: {method} {url}"
        ) from last_exc

    @staticmethod
    def format_analyte_selection(analytes: Union[str, List[str], Tuple[str, ...]]) -> str:
        """Format analyte input into a comma-separated string.

        The WAMEX backend expects either 'All' (no filter) or a comma-separated
        list of analyte symbols. User entering 'all' (any case) is normalised
        to 'All'. Empty input also means 'All'.
        """
        if not analytes:
            return "All"
        if isinstance(analytes, (list, tuple, set)):
            items = [str(a).strip() for a in analytes if str(a).strip()]
            if not items:
                return "All"
            if len(items) == 1 and items[0].lower() == "all":
                return "All"
            return ", ".join(items)
        analyte_str = str(analytes).strip()
        if not analyte_str or analyte_str.lower() == "all":
            return "All"
        return analyte_str

    @staticmethod
    def format_limit(limit: Union[int, str]) -> str:
        """Validate and map limit value to valid backend options.

        Backend-accepted values (per the WAMEX form HTML):
            100, 500, 1000, 10000, 100000, -1 ('All').

        Rules:
        - '-1', 'all' (any case) and any negative number -> '-1' (ALL records).
          -1 is NEVER silently replaced by a smaller cap.
        - Non-numeric input raises ValueError (never silently becomes -1).
        - Positive values snap DOWN to the nearest valid threshold
          (e.g. 250 -> 100, 2500 -> 1000, 200000 -> 100000).
        """
        if isinstance(limit, str):
            cleaned = limit.strip().replace(",", "").replace("_", "")
            if not cleaned:
                raise ValueError("Record limit is empty. Use -1 for ALL records, "
                                 "or one of: 100, 500, 1000, 10000, 100000.")
            if cleaned.lower() in ("all", "-1"):
                return "-1"
            try:
                lim_int = int(cleaned)
            except ValueError:
                raise ValueError(
                    f"Invalid record limit {limit!r}. Use -1 for ALL records, "
                    f"or one of: 100, 500, 1000, 10000, 100000."
                )
        else:
            try:
                lim_int = int(limit)
            except (ValueError, TypeError):
                raise ValueError(
                    f"Invalid record limit {limit!r}. Use -1 for ALL records, "
                    f"or one of: 100, 500, 1000, 10000, 100000."
                )

        if lim_int == -1 or lim_int < 0:
            return "-1"
        if lim_int in VALID_LIMITS:
            return str(lim_int)
        for threshold in [100, 500, 1000, 10000, 100000]:
            if lim_int <= threshold:
                return str(threshold)
        return "-1"

    @staticmethod
    def _extract_select_options(html: str, field_name: str) -> Dict[str, str]:
        """Extract options from a named <select> element in WAMEX HTML.

        Returns a dict mapping lookup keys -> exact_server_value, where the
        lookup keys cover BOTH the option's `value` attribute AND its visible
        display text (lowercased).  This guarantees we never assume that the
        displayed name equals the backend value: whichever the user supplies,
        we send the exact backend `value` back to the server.
        """
        pattern = rf'<select[^>]*name="{re.escape(field_name)}"[^>]*>(.*?)</select>'
        select_match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if not select_match:
            return {}
        opts: Dict[str, str] = {}
        for opt_html in re.findall(r"<option[^>]*>(?:(?!</option>).)*</option>",
                                   select_match.group(1), re.DOTALL | re.IGNORECASE):
            val_m = re.search(r'value="([^"]*)"', opt_html)
            opt_value = (val_m.group(1) if val_m else "").strip()
            # Visible text is everything between '>' and '</option>'
            text_m = re.match(r"<option[^>]*>(.*)</option>", opt_html,
                              re.DOTALL | re.IGNORECASE)
            opt_text = (text_m.group(1) if text_m else "").strip()
            if opt_value:
                opts[opt_value.lower()] = opt_value
            if opt_text and opt_text.lower() != opt_value.lower():
                opts[opt_text.lower()] = opt_value
        return opts

    @staticmethod
    def normalise_select_value(
        user_value: str,
        available_options: Dict[str, str],
        default: str = "All",
        field_label: str = "value",
    ) -> str:
        """Normalise a user-supplied dropdown value to the exact server-expected string.

        Rules:
        - Blank / whitespace-only  -> default (no filter)
        - 'all' (any case)         -> default (no filter)
        - Exact/case-insensitive match (value OR display text) -> server's exact value
        - Options loaded but NO match -> raise InvalidFilterError with the closest
          valid suggestions (prevents a typo being silently reported as
          'No records found').
        - Options dict EMPTY (page parse failed) -> warn loudly and pass the value
          through unchanged (cannot validate without the option list).
        """
        cleaned = user_value.strip() if user_value else ""
        if not cleaned or cleaned.lower() == "all":
            return default
        if not available_options:
            print(
                f"  [!] WARNING: could not load the {field_label} option list from "
                f"the server, so '{cleaned}' could not be validated. Sending as-is."
            )
            return cleaned
        matched = available_options.get(cleaned.lower())
        if matched is not None:
            return matched
        # No match — compute the closest valid options for a helpful error.
        suggestions = difflib.get_close_matches(
            cleaned, list(available_options.keys()), n=5, cutoff=0.55
        )
        canonical_suggestions = sorted({available_options[s] for s in suggestions})
        raise InvalidFilterError(
            f"Invalid {field_label} {cleaned!r}: it does not match any option "
            f"currently offered by the WAMEX website. "
            f"(Did you mean: {', '.join(canonical_suggestions) if canonical_suggestions else 'n/a'}? "
            f"Leave blank or use 'All' for no filter.)",
            suggestions=canonical_suggestions,
        )

    @staticmethod
    def _extract_query_uuid(html: str) -> Optional[str]:
        """Extract the query UUID from WAMEX server response HTML.

        WAMEX embeds the UUID in different ways:
        1. Inside start_download() call (may be in an HTML comment)
        2. As a bare /start_zip/UUID path in href or data attributes

        Both are valid; the UUID is the same in all cases.
        """
        # Pattern 1: inside start_download() call (including HTML-commented variants)
        m = re.search(
            r"""start_download\s*\(\s*['"/]*/start_zip/([a-zA-Z0-9_-]+)['"]""",
            html
        )
        if m:
            return m.group(1)
        # Pattern 2: bare path anywhere in the page
        m = re.search(r"/start_zip/([a-zA-Z0-9_-]+)", html)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _has_no_results(html: str, is_downhole: bool) -> bool:
        """Return True if the server genuinely returned zero records.

        CRITICAL: The string 'Found No Results' appears in EVERY WAMEX page
        as a JavaScript template literal inside the autocomplete widget:

            message.innerHTML = `Found No Results for "${query}"`;

        This string is hardcoded in the page JS regardless of query results.
        It MUST NOT be used to detect zero-result queries.

        Instead:
        - If /start_zip/UUID is present anywhere -> data was found (return False)
        - If 'No search conducted' appears in the results panel -> no data (return True)
        """
        # UUID presence is the most reliable "found data" indicator
        if re.search(r"/start_zip/[a-zA-Z0-9_-]+", html):
            return False

        # 'No search conducted' only appears in the visible results panel
        # when the server found nothing, NOT inside <script> blocks.
        m = re.search(
            r'Search results.*?(?:Collars|Sites|Surface Samples)(.*?)(?:assay-results|END HTML PAGE)',
            html, re.DOTALL | re.IGNORECASE
        )
        results_section = m.group(1) if m else html
        return "No search conducted" in results_section

    @staticmethod
    def _extract_analyte_names(html: str) -> List[str]:
        """Dynamically extract the analyte name list embedded in the page JS.

        The WAMEX query pages embed `const ANALYTES = [...];` which drives the
        analyte autocomplete widget. Extracting it keeps the tool generic — no
        analyte names are hardcoded here.
        """
        m = re.search(r"const\s+ANALYTES\s*=\s*\[(.*?)\]\s*;", html, re.DOTALL)
        if not m:
            return []
        names = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
        return [n.strip() for n in names if n.strip()]

    @staticmethod
    def normalise_analytes(
        analytes: Union[str, List[str], Tuple[str, ...], None],
        available_analytes: Optional[List[str]] = None,
    ) -> str:
        """Normalise user analyte input to the exact format WAMEX expects.

        Mirrors the site's own JavaScript (updateHiddenInput):
        - blank / 'all' (any case)              -> 'All'   (no filter)
        - multiple analytes                     -> sorted alphabetically,
                                                   joined with ', '
        - 'All' selected together with others   -> 'All'
        - each token is case-normalised against the live ANALYTES list when
          available (e.g. 'au' -> 'Au'); unknown tokens pass through with a
          warning.

        Accepts a comma-separated string, a list/tuple/set, or None.
        """
        if analytes is None:
            return "All"
        if isinstance(analytes, str):
            tokens = [t.strip() for t in analytes.split(",") if t.strip()]
        elif isinstance(analytes, (list, tuple, set)):
            tokens: List[str] = []
            for item in analytes:
                for t in str(item).split(","):
                    t = t.strip()
                    if t:
                        tokens.append(t)
        else:
            tokens = [str(analytes).strip()]

        if not tokens:
            return "All"

        lowered = [t.lower() for t in tokens]
        if "all" in lowered:
            return "All"

        lookup = {a.lower(): a for a in (available_analytes or [])}
        normalised: List[str] = []
        for tok in tokens:
            canonical = lookup.get(tok.lower())
            if canonical is not None:
                normalised.append(canonical)
            else:
                if available_analytes:
                    near = difflib.get_close_matches(
                        tok, available_analytes, n=3, cutoff=0.6
                    )
                    hint = f" (did you mean {', '.join(near)}?)" if near else ""
                    print(f"  [!] WARNING: analyte {tok!r} is not in the "
                          f"server's analyte list{hint}. Sending as-is.")
                normalised.append(tok)

        # Deduplicate (case-insensitive) and sort alphabetically, like the site JS
        seen = set()
        unique: List[str] = []
        for tok in normalised:
            key = tok.lower()
            if key not in seen:
                seen.add(key)
                unique.append(tok)
        return ", ".join(sorted(unique, key=str.casefold))

    @staticmethod
    def format_coordinates(
        boundary_1: Optional[Union[str, Tuple[float, float], List[float]]] = None,
        boundary_2: Optional[Union[str, Tuple[float, float], List[float]]] = None,
        bbox: Optional[Union[Tuple[float, float, float, float], List[float]]] = None
    ) -> Tuple[str, str]:
        """
        Convert spatial inputs into WAMEX's required Leaflet LatLng representation:
        'LatLng(latitude, longitude)'
        """
        def to_latlng_str(val: Any) -> str:
            if not val:
                return ""
            if isinstance(val, str):
                s = val.strip()
                if not s:
                    return ""
                if s.startswith("LatLng(") and s.endswith(")"):
                    return s
                parts = s.replace("(", "").replace(")", "").split(",")
                if len(parts) == 2:
                    try:
                        return f"LatLng({float(parts[0].strip())}, {float(parts[1].strip())})"
                    except ValueError:
                        return s
                return s
            if isinstance(val, (tuple, list)) and len(val) >= 2:
                return f"LatLng({float(val[0])}, {float(val[1])})"
            return ""

        if bbox is not None and len(bbox) == 4:
            min_lat, min_lon, max_lat, max_lon = bbox
            return f"LatLng({min_lat}, {min_lon})", f"LatLng({max_lat}, {max_lon})"

        return to_latlng_str(boundary_1), to_latlng_str(boundary_2)

    @staticmethod
    def count_csv_records(file_path: str) -> int:
        """Count rows in a CSV file excluding header line."""
        if not os.path.exists(file_path):
            return 0
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f if line.strip()]
        return max(0, len(lines) - 1)

    @staticmethod
    def apply_depth_filter(file_path: str, min_depth: Optional[float] = None, max_depth: Optional[float] = None) -> int:
        """
        Filter CSV rows based on depth columns (fromdepth, maxdepth, depth_from).
        Overwrites file with filtered rows.
        """
        if min_depth is None and max_depth is None:
            return WamexDownloader.count_csv_records(file_path)

        if not os.path.exists(file_path):
            return 0

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = list(csv.reader(f))

        if not reader:
            return 0

        header = reader[0]
        depth_col_idx = None
        for candidate in ["fromdepth", "maxdepth", "depth_from", "depth_to"]:
            for idx, col in enumerate(header):
                if candidate == col.strip().lower():
                    depth_col_idx = idx
                    break
            if depth_col_idx is not None:
                break

        if depth_col_idx is None:
            return len(reader) - 1

        filtered_rows = [header]
        for row in reader[1:]:
            if len(row) > depth_col_idx:
                raw_val = row[depth_col_idx].strip()
                if not raw_val:
                    filtered_rows.append(row)
                    continue
                try:
                    val = float(raw_val)
                    if min_depth is not None and val < min_depth:
                        continue
                    if max_depth is not None and val > max_depth:
                        continue
                    filtered_rows.append(row)
                except ValueError:
                    filtered_rows.append(row)
            else:
                filtered_rows.append(row)

        with open(file_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(filtered_rows)

        return len(filtered_rows) - 1

    def download_dataset(
        self,
        query_type: str = "surface",
        area: str = "",
        analytes: Union[str, List[str]] = "",
        hole_or_sample_type: str = "",
        tenement: str = "",
        company: str = "",
        report_number: str = "",
        company_id: str = "",
        limit: Union[int, str] = -1,
        boundary_1: Optional[Union[str, Tuple[float, float], List[float]]] = None,
        boundary_2: Optional[Union[str, Tuple[float, float], List[float]]] = None,
        bbox: Optional[Union[Tuple[float, float, float, float], List[float]]] = None,
        polygon_file: Optional[str] = None,
        min_depth: Optional[float] = None,
        max_depth: Optional[float] = None,
        additional_dh_tables: Optional[List[str]] = None,
        output_dir: str = "WAMEX_data",
        force_download: bool = True,
        verbose: bool = True
    ) -> Dict[str, Any]:
        """
        Execute query against WAMEX portal and download resulting dataset.

        Empty string for area/analytes/hole_or_sample_type/tenement/company
        means NO FILTER (equivalent to selecting 'All' on the website).
        Passing 'all' (any case) is also normalised to no-filter.
        """
        q_type = query_type.strip().lower()
        if q_type in ["dh", "downhole", "drillhole"]:
            is_downhole = True
            norm_qtype = "downhole"
            page_endpoint = "/dh_query_page"
            folder_name = "downhole"
        else:
            is_downhole = False
            norm_qtype = "surface"
            page_endpoint = "/ss_query_page"
            folder_name = "surface"

        page_url = f"{BASE_URL}{page_endpoint}"

        if verbose:
            print(f"\n[1/5] Accessing {norm_qtype.upper()} query interface ({page_url})...")

        page_resp = self.session.get(page_url, timeout=45)
        if page_resp.status_code != 200:
            raise RuntimeError(
                f"Failed to access query page: HTTP {page_resp.status_code}"
            )
        page_html = page_resp.text

        # Extract CSRF token — try both attribute orderings seen in WAMEX HTML
        csrf_match = re.search(
            r'name="csrf_token"\s+type="hidden"\s+value="([^"]+)"', page_html
        )
        if not csrf_match:
            csrf_match = re.search(
                r'id="csrf_token"[^>]*value="([^"]+)"', page_html
            )
        if not csrf_match:
            raise RuntimeError("Could not find CSRF token on query page.")
        csrf_token = csrf_match.group(1)

        # Dynamically read dropdown option values from the live WAMEX form HTML.
        # This ensures we always use the exact server-expected casing.
        available_areas = self._extract_select_options(page_html, "area")
        type_field_name = "hole_type" if is_downhole else "sample_type"
        available_types = self._extract_select_options(page_html, type_field_name)

        if verbose and available_areas:
            print(f"      Found {len(available_areas)} mapsheet options from server.")

        # Normalise user values: blank/"all" -> "All"; otherwise match server casing
        matched_area = self.normalise_select_value(
            str(area or ""), available_areas, default="All"
        )
        matched_type = self.normalise_select_value(
            str(hole_or_sample_type or ""), available_types, default="All"
        )

        raw_comp = str(company or "").strip()
        comp_val = "All" if not raw_comp or raw_comp.lower() == "all" else raw_comp

        raw_ten = str(tenement or "").strip()
        ten_val = "All" if not raw_ten or raw_ten.lower() == "all" else raw_ten

        b1_val, b2_val = self.format_coordinates(boundary_1=boundary_1, boundary_2=boundary_2, bbox=bbox)
        analyte_val = self.format_analyte_selection(analytes)
        limit_val = self.format_limit(limit)

        # Build output folder name: <Area>_DH or <Area>_SS
        # matched_area is "All" when no area filter was supplied; use "ALL" as the label.
        area_label = matched_area if matched_area != "All" else "ALL"
        # Sanitise for filesystem: replace anything that is not alphanumeric/dot/hyphen with '_'
        area_label = re.sub(r'[^\w.-]', '_', area_label).strip('_') or "ALL"
        type_suffix = "DH" if is_downhole else "SS"
        folder_name = f"{area_label}_{type_suffix}"
        final_output_dir = os.path.join(output_dir, folder_name)
        os.makedirs(final_output_dir, exist_ok=True)

        meta_filename = f"{norm_qtype}_execution_metadata.json"
        meta_path = os.path.join(final_output_dir, meta_filename)

        current_filters = {
            "area": matched_area,
            "analytes": analyte_val,
            "hole_or_sample_type": matched_type,
            "tenement": ten_val,
            "company": comp_val,
            "report_number": str(report_number or "").strip(),
            "company_id": str(company_id or "").strip(),
            "limit": limit_val,
            "boundary_1": b1_val,
            "boundary_2": b2_val,
            "polygon_file": polygon_file,
            "min_depth": min_depth,
            "max_depth": max_depth,
            "additional_dh_tables": additional_dh_tables or []
        }

        # Skip download if an identical query result already exists on disk
        if not force_download and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as mf:
                    existing_meta = json.load(mf)
                if existing_meta.get("filters") == current_filters:
                    rec_summary = existing_meta.get("records", {})
                    if verbose:
                        print(f"\n[*] Matching data already exists in {final_output_dir}. Skipping duplicate download.")
                        print(f"    Previous execution timestamp: {existing_meta.get('timestamp')}")
                        for fn, cnt in rec_summary.items():
                            print(f"    * {fn:<25}: {cnt:,} records")
                    return {
                        "status": "cached",
                        "query_uuid": existing_meta.get("query_uuid"),
                        "query_type": norm_qtype,
                        "output_dir": os.path.abspath(final_output_dir),
                        "records": rec_summary,
                        "files": {fn: os.path.join(final_output_dir, fn) for fn in rec_summary}
                    }
            except Exception:
                pass

        if is_downhole:
            payload = {
                "csrf_token": csrf_token,
                "company": current_filters["company"],
                "hole_type": current_filters["hole_or_sample_type"],
                "tenement": current_filters["tenement"],
                "area": current_filters["area"],
                "report_number": current_filters["report_number"],
                "company_hole_id": current_filters["company_id"],
                "analyte_selection": current_filters["analytes"],
                "boundary_1": current_filters["boundary_1"],
                "boundary_2": current_filters["boundary_2"],
                "hole_id": "All",
                "dh_collar_id": "",
                "previously_uploaded_file": "",
                "number_holes": current_filters["limit"]
            }
        else:
            payload = {
                "csrf_token": csrf_token,
                "company": current_filters["company"],
                "sample_type": current_filters["hole_or_sample_type"],
                "tenement": current_filters["tenement"],
                "area": current_filters["area"],
                "report_number": current_filters["report_number"],
                "company_sample_id": current_filters["company_id"],
                "analyte_selection": current_filters["analytes"],
                "boundary_1": current_filters["boundary_1"],
                "boundary_2": current_filters["boundary_2"],
                "sample_id": "",
                "previously_uploaded_file": "",
                "number_holes": current_filters["limit"]
            }

        files = None
        opened_file = None
        if polygon_file and os.path.exists(polygon_file):
            opened_file = open(polygon_file, "rb")
            files = {"polygon_file": (os.path.basename(polygon_file), opened_file, "application/json")}

        if verbose:
            print(f"\n[2/5] Submitting query parameters:")
            print(f"      - Dataset:             {norm_qtype.upper()}")
            print(f"      - Area/Mapsheet:       {payload['area']}")
            print(f"      - Analytes:            {payload['analyte_selection']}")
            print(f"      - Hole/Sample Type:    {payload.get('hole_type') or payload.get('sample_type')}")
            print(f"      - Tenement:            {payload['tenement']}")
            print(f"      - Company:             {payload['company']}")
            print(f"      - Report Number:       {payload['report_number'] or '(None)'}")
            print(f"      - Company ID:          {payload.get('company_hole_id') or payload.get('company_sample_id') or '(None)'}")
            print(f"      - Record Limit:        {payload['number_holes']} ({'All matching records' if limit_val == '-1' else limit_val})")
            if b1_val and b2_val:
                print(f"      - Bounding Box:        {b1_val} to {b2_val}")
            if polygon_file:
                print(f"      - Polygon File:        {polygon_file}")
            if min_depth is not None or max_depth is not None:
                print(f"      - Depth Filter:        min={min_depth}m, max={max_depth}m")

        try:
            search_resp = self.session.post(page_url, data=payload, files=files, timeout=180)
        finally:
            if opened_file:
                opened_file.close()

        if search_resp.status_code != 200:
            raise RuntimeError(f"Search request failed: HTTP {search_resp.status_code}")

        search_html = search_resp.text

        # ---------------------------------------------------------------
        # Extract UUID or detect genuine no-results
        # ---------------------------------------------------------------
        # NOTE: "Found No Results" appears in EVERY page as part of the
        # tenement autocomplete JS template and MUST NOT be used to detect
        # zero-result queries. Use _extract_query_uuid and _has_no_results.
        # ---------------------------------------------------------------
        query_uuid = self._extract_query_uuid(search_html)

        if not query_uuid:
            genuinely_empty = self._has_no_results(search_html, is_downhole)
            if genuinely_empty:
                if verbose:
                    if "No search conducted" in search_html:
                        print(
                            "[-] Server returned 'No search conducted' — "
                            "the filter combination produced no collar/site records."
                        )
                        if matched_area != "All":
                            print(
                                f"    Mapsheet '{matched_area}' may have no records "
                                f"for the selected filters."
                            )
                    else:
                        print("[-] No records found matching the specified criteria.")
                return {
                    "status": "no_results",
                    "records": {},
                    "output_dir": os.path.abspath(final_output_dir),
                    "collar_count": 0,
                    "assay_count": 0,
                }
            # UUID missing but page doesn't indicate zero results — unexpected
            snippet = search_html[:2000].replace("\n", " ")
            raise RuntimeError(
                f"Could not extract query UUID from server response "
                f"(len={len(search_html)}). "
                f"Page snippet: {snippet!r}"
            )

        if verbose:
            print(f"      Query accepted by server. UUID: {query_uuid}")

        # Request packaging
        if verbose:
            print(f"\n[3/5] Requesting data archive packaging...")
        start_url = f"{BASE_URL}/start_zip/{query_uuid}"
        options_payload = {"options": additional_dh_tables or []}
        try:
            self.session.post(start_url, json=options_payload, timeout=30)
        except (requests.exceptions.Timeout, requests.exceptions.RequestException):
            # WAMEX triggers packaging asynchronously; continue polling even if
            # the HTTP read times out.
            pass

        # Poll ZIP status
        check_url = f"{BASE_URL}/check_zip/{query_uuid}"
        redirect_path = None
        if verbose:
            print(f"      Waiting for server archive preparation...")

        last_complete = -1
        for attempt in range(1, 600):
            time.sleep(3)
            try:
                check_resp = self.session.get(check_url, timeout=30)
            except requests.exceptions.RequestException as exc:
                if verbose:
                    print(f"      [!] Polling error (attempt {attempt}): {exc}")
                continue
            if check_resp.status_code == 200:
                try:
                    status_data = check_resp.json()
                    if status_data.get("redirect"):
                        redirect_path = status_data["redirect"]
                        if verbose:
                            print(f"      Archive ready: {redirect_path}")
                        break
                    elif status_data.get("timeout"):
                        raise TimeoutError(
                            "Server reported query exceeded execution time limit."
                        )
                    else:
                        complete = status_data.get("complete", 0)
                        total = status_data.get("total", 0)
                        if verbose and complete != last_complete:
                            if total > 0:
                                pct = (complete / total) * 100
                                print(
                                    f"      Progress: {complete:,} / {total:,} records "
                                    f"packaged ({pct:.1f}%)..."
                                )
                            else:
                                print(
                                    f"      Progress: {complete:,} records processed..."
                                )
                            last_complete = complete
                except json.JSONDecodeError:
                    pass
            elif check_resp.status_code == 404:
                raise RuntimeError(
                    f"Server returned 404 for check_zip/{query_uuid}. "
                    f"Query result may have expired or packaging failed."
                )

        if not redirect_path:
            raise TimeoutError(
                f"Timed out after {attempt * 3}s waiting for server to prepare "
                f"data package (UUID: {query_uuid})."
            )

        # Download archive
        download_url = f"{BASE_URL}{redirect_path}"
        if verbose:
            print(f"\n[4/5] Downloading archive from {download_url}...")
        dl_resp = self.session.get(download_url, timeout=300)
        if dl_resp.status_code != 200:
            raise RuntimeError(
                f"Failed to download archive: HTTP {dl_resp.status_code} "
                f"from {download_url}"
            )
        if verbose:
            print(f"      Downloaded {len(dl_resp.content):,} bytes.")

        # Extract files
        if verbose:
            print(f"\n[5/5] Extracting files to: {os.path.abspath(final_output_dir)}...")

        try:
            z = zipfile.ZipFile(io.BytesIO(dl_resp.content))
        except zipfile.BadZipFile as exc:
            raise RuntimeError(
                f"Downloaded file is not a valid ZIP archive: {exc}. "
                f"First 200 bytes: {dl_resp.content[:200]!r}"
            )
        extracted_summary: Dict[str, int] = {}
        extracted_paths: Dict[str, str] = {}

        for filename in z.namelist():
            file_bytes = z.read(filename)
            dest_path = os.path.join(final_output_dir, filename)

            with open(dest_path, "wb") as f:
                f.write(file_bytes)

            extracted_paths[filename] = dest_path

            if filename.endswith(".csv"):
                initial_count = self.count_csv_records(dest_path)
                if min_depth is not None or max_depth is not None:
                    final_count = self.apply_depth_filter(dest_path, min_depth, max_depth)
                    if verbose:
                        print(f"      [+] {filename:<25}: {final_count:,} records (depth filtered from {initial_count:,})")
                    extracted_summary[filename] = final_count
                else:
                    if verbose:
                        print(f"      [+] {filename:<25}: {initial_count:,} records")
                    extracted_summary[filename] = initial_count
            else:
                if verbose:
                    print(f"      [+] {filename:<25}: (Metadata / JSON)")

        metadata = {
            "query_type": norm_qtype,
            "query_uuid": query_uuid,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "filters": current_filters,
            "records": extracted_summary
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=4)

        return {
            "status": "success",
            "query_uuid": query_uuid,
            "query_type": norm_qtype,
            "output_dir": os.path.abspath(final_output_dir),
            "records": extracted_summary,
            "files": extracted_paths
        }

    def download(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute downloads for specified datasets ('surface', 'downhole', or 'both').
        """
        q_type = str(config.get("query_type", "both")).strip().lower()
        output_base = config.get("output_dir", "WAMEX_data")
        results = {}

        if q_type in ["both", "all"]:
            datasets_to_run = ["downhole", "surface"]
        elif q_type in ["dh", "downhole", "drillhole"]:
            datasets_to_run = ["downhole"]
        else:
            datasets_to_run = ["surface"]

        for dset in datasets_to_run:
            if config.get("verbose", True):
                print("\n" + "=" * 70)
                print(f"   STARTING DOWNLOAD: {dset.upper()} DATA")
                print("=" * 70)

            hole_or_sample = config.get("hole_or_sample_type", "")
            if dset == "downhole" and "hole_type" in config:
                hole_or_sample = config["hole_type"]
            elif dset == "surface" and "sample_type" in config:
                hole_or_sample = config["sample_type"]

            res = self.download_dataset(
                query_type=dset,
                area=config.get("area", ""),
                analytes=config.get("analytes", ""),
                hole_or_sample_type=hole_or_sample,
                tenement=config.get("tenement", "All"),
                company=config.get("company", "All"),
                report_number=config.get("report_number", ""),
                company_id=config.get("company_id", ""),
                limit=config.get("limit", -1),
                boundary_1=config.get("boundary_1"),
                boundary_2=config.get("boundary_2"),
                bbox=config.get("bbox"),
                polygon_file=config.get("polygon_file"),
                min_depth=config.get("min_depth"),
                max_depth=config.get("max_depth"),
                additional_dh_tables=config.get("additional_dh_tables"),
                output_dir=output_base,
                force_download=config.get("force_download", False),
                verbose=config.get("verbose", True)
            )
            results[dset] = res

        return results


def download_wamex_data(config: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience functional wrapper."""
    downloader = WamexDownloader()
    return downloader.download(config)


def prompt_user_for_config() -> Dict[str, Any]:
    """
    Interactive terminal prompt wizard to collect filter values from the user at runtime.
    Leaving any field blank applies no filter.
    """
    print("\n" + "=" * 70)
    print("        WAMEX GEOCHEMISTRY DATA DOWNLOADER - QUERY WIZARD")
    print("=" * 70)
    print("Press [Enter] on any prompt to leave it blank (no filter / query all).\n")

    # 1. Dataset Type
    print("Dataset Type:")
    print("  1. Surface assays")
    print("  2. Downhole assays")
    print("  3. Both (Surface and Downhole)")
    choice = input("Enter choice [1/2/3] (default: 3 - Both): ").strip()
    if choice == "1":
        query_type = "surface"
    elif choice == "2":
        query_type = "downhole"
    else:
        query_type = "both"

    # 2. Area / Mapsheet
    print("\n--- Mapsheet / Area ---")
    print("Available values are loaded dynamically from WAMEX at query time.")
    print("Examples: Kalgoorlie, Leonora, Menzies, Laverton, Sandstone, Norseman ...")
    area_input = input(
        "1:250,000 Mapsheet Area name (or blank for All): "
    ).strip()
    area = area_input  # Empty string = no filter (normalised to 'All' at query time)

    # 3. Analytes
    analytes_input = input(
        "\nAnalytes / Elements (comma-separated, e.g. Au,Cu,Ni or blank for All): "
    ).strip()
    analytes = analytes_input  # Empty = no filter

    # 4. Hole or Sample Type
    if query_type == "downhole":
        type_prompt = (
            "Hole Type for Downhole — available: All, Core, Non-Core, AC, AUG, "
            "PERC, RAB, RC, RM, UNKN (or blank for All): "
        )
    elif query_type == "surface":
        type_prompt = (
            "Sample Type for Surface — available: All, DRILLSPOIL, MINEDUMP, "
            "ROCKCHIP, SHALLOWSURFDRILL, SOIL, STREAMSED, UNKN, VEGETATION, "
            "WATER (or blank for All): "
        )
    else:
        type_prompt = (
            "Hole/Sample Type (or blank for All). "
            "For DH: Core/RC/RAB etc. For SS: SOIL/ROCKCHIP/STREAMSED etc.: "
        )
    type_input = input(type_prompt).strip()
    hole_or_sample_type = type_input  # Empty = no filter

    # 5. Tenement ID
    tenement_input = input(
        "\nTenement ID (e.g. E0401441, or blank for All): "
    ).strip()
    tenement = tenement_input  # Empty = no filter

    # 6. Company Name
    company_input = input(
        "Company Name (partial or complete, case insensitive, or blank for All): "
    ).strip()
    company = company_input  # Empty = no filter

    # 7. WAMEX Report Number
    report_number_input = input(
        "WAMEX Report Number (A-Number, or blank for no filter): "
    ).strip()
    report_number = report_number_input

    # 8. Company Hole ID or Sample ID
    company_id_input = input(
        "Company Hole ID or Sample ID (or blank for no filter): "
    ).strip()
    company_id = company_id_input

    # 9. Record Limit
    print("\nRecord Limit options:")
    print("  -1 = ALL matching records (may be slow for large datasets)")
    print("  100, 500, 1000, 10000, 100000")
    limit_input = input("Enter Record Limit (default: 100 for a quick test): ").strip()
    limit = limit_input if limit_input else "100"

    # 10. Spatial Boundaries
    boundary_1 = None
    boundary_2 = None
    bbox = None
    polygon_file = None

    print("\nSpatial Boundary options:")
    print("  0. None (Whole area / no spatial boundary)")
    print("  1. Bounding box coordinates (min_lat min_lon max_lat max_lon)")
    print("  2. Two corner coordinates (Boundary 1 and Boundary 2)")
    print("  3. Path to GeoJSON polygon boundary file")
    spatial_choice = input("Enter spatial boundary option [0/1/2/3] (default: 0): ").strip()

    if spatial_choice == "1":
        bbox_raw = input("Enter bounding box coordinates (min_lat min_lon max_lat max_lon): ").strip()
        if bbox_raw:
            parts = bbox_raw.replace(",", " ").split()
            if len(parts) == 4:
                try:
                    bbox = [float(p) for p in parts]
                except ValueError:
                    print("Could not parse coordinates; skipping spatial bounding box.")
    elif spatial_choice == "2":
        b1_raw = input("Enter Boundary 1 (lat, lon): ").strip()
        b2_raw = input("Enter Boundary 2 (lat, lon): ").strip()
        if b1_raw and b2_raw:
            boundary_1 = b1_raw
            boundary_2 = b2_raw
    elif spatial_choice == "3":
        poly_path = input("Enter path to local GeoJSON file: ").strip()
        if poly_path and os.path.exists(poly_path):
            polygon_file = poly_path
        elif poly_path:
            print(f"File not found: {poly_path}; skipping polygon boundary.")

    # 11. Depth Filter (Downhole only)
    min_depth = None
    max_depth = None
    if query_type in ["downhole", "both"]:
        min_d_raw = input("\nMinimum depth in meters (or blank for no minimum): ").strip()
        if min_d_raw:
            try:
                min_depth = float(min_d_raw)
            except ValueError:
                pass
        max_d_raw = input("Maximum depth in meters (or blank for no maximum): ").strip()
        if max_d_raw:
            try:
                max_depth = float(max_d_raw)
            except ValueError:
                pass

    # 12. Output Directory
    out_dir_raw = input("\nOutput directory (default: WAMEX_data): ").strip()
    output_dir = out_dir_raw if out_dir_raw else "WAMEX_data"

    # 13. Force re-download
    force_raw = input("Force re-download if existing matching files exist? [y/N] (default: N): ").strip().lower()
    force_download = force_raw in ["y", "yes", "true", "1"]

    return {
        "query_type": query_type,
        "area": area,          # empty string = no area filter
        "analytes": analytes,  # empty string = all analytes
        "hole_or_sample_type": hole_or_sample_type,  # empty = all types
        "tenement": tenement,  # empty = all tenements
        "company": company,    # empty = all companies
        "report_number": report_number,
        "company_id": company_id,
        "limit": limit,
        "boundary_1": boundary_1,
        "boundary_2": boundary_2,
        "bbox": bbox,
        "polygon_file": polygon_file,
        "min_depth": min_depth,
        "max_depth": max_depth,
        "additional_dh_tables": [],
        "output_dir": output_dir,
        "force_download": force_download,
        "verbose": True,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="WAMEX Geochemistry Data Downloader (Downhole & Surface)"
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Launch interactive prompt wizard to enter query filters at runtime"
    )
    parser.add_argument(
        "-d", "--dataset",
        choices=["surface", "ss", "downhole", "dh", "both"],
        default=None,
        help="Dataset type: 'surface', 'downhole', or 'both'"
    )
    parser.add_argument(
        "-a", "--area",
        default=None,
        help="1:250k Mapsheet area name (or 'All')"
    )
    parser.add_argument(
        "--analytes",
        default=None,
        help="Comma-separated analytes/elements (or 'All')"
    )
    parser.add_argument(
        "--type",
        dest="hole_or_sample_type",
        default=None,
        help="Hole type (DH) or Sample type (Surface) (or 'All')"
    )
    parser.add_argument(
        "-t", "--tenement",
        default=None,
        help="Tenement ID (or 'All')"
    )
    parser.add_argument(
        "-c", "--company",
        default=None,
        help="Company name (or 'All')"
    )
    parser.add_argument(
        "-l", "--limit",
        default=None,
        help="Maximum records to return (-1 for ALL matching records, or 100, 500, 1000, 10000)"
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"),
        help="Spatial bounding box corners: min_lat min_lon max_lat max_lon"
    )
    parser.add_argument(
        "--boundary-1",
        dest="boundary_1",
        default=None,
        help="Boundary corner 1 (format: 'lat, lon')"
    )
    parser.add_argument(
        "--boundary-2",
        dest="boundary_2",
        default=None,
        help="Boundary corner 2 (format: 'lat, lon')"
    )
    parser.add_argument(
        "--polygon-file",
        help="Path to GeoJSON file defining polygon boundary"
    )
    parser.add_argument(
        "--min-depth",
        type=float,
        help="Minimum depth filter in meters (post-query, DH only)"
    )
    parser.add_argument(
        "--max-depth",
        type=float,
        help="Maximum depth filter in meters (post-query, DH only)"
    )
    parser.add_argument(
        "--report-number",
        default=None,
        help="WAMEX report number (A-Number)"
    )
    parser.add_argument(
        "--company-id",
        default=None,
        help="Company Hole ID or Sample ID"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="Directory to save downloaded files (default: WAMEX_data)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if matching query has already been downloaded"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Determine whether to run interactive prompt or use command-line parameters.
    # If run without arguments, or with -i/--interactive, launch interactive wizard.
    has_cli_filter = any([
        args.dataset is not None,
        args.area is not None,
        args.analytes is not None,
        args.hole_or_sample_type is not None,
        args.tenement is not None,
        args.company is not None,
        args.limit is not None,
        args.bbox is not None,
        args.boundary_1 is not None,
        args.boundary_2 is not None,
        args.polygon_file is not None,
        args.min_depth is not None,
        args.max_depth is not None,
        args.report_number is not None,
        args.company_id is not None,
        args.output_dir is not None
    ])

    if args.interactive or not has_cli_filter:
        config = prompt_user_for_config()
    else:
        config = {
            "query_type": args.dataset or "both",
            # Empty string = no filter (normalised to 'All' internally)
            "area": args.area or "",
            "analytes": args.analytes or "",
            "hole_or_sample_type": args.hole_or_sample_type or "",
            "tenement": args.tenement or "",
            "company": args.company or "",
            "report_number": args.report_number or "",
            "company_id": args.company_id or "",
            "limit": args.limit if args.limit is not None else "-1",
            "boundary_1": args.boundary_1,
            "boundary_2": args.boundary_2,
            "bbox": args.bbox,
            "polygon_file": args.polygon_file,
            "min_depth": args.min_depth,
            "max_depth": args.max_depth,
            "additional_dh_tables": [],
            "output_dir": args.output_dir or "WAMEX_data",
            "force_download": args.force,
            "verbose": True,
        }

    print("\n" + "=" * 70)
    print("        WAMEX GEOCHEMISTRY TARGETED DATA DOWNLOADER")
    print("=" * 70)

    results = download_wamex_data(config)

    print("\n" + "=" * 70)
    print("DOWNLOAD SUMMARY:")
    print("=" * 70)
    for dset_key, res in results.items():
        print(f"\n--- {dset_key.upper()} DATASET ---")
        print(f"  Status:         {res.get('status')}")
        if res.get("query_uuid"):
            print(f"  Query UUID:     {res.get('query_uuid')}")
        print(f"  Target Folder:  {res.get('output_dir')}")
        if res.get("records"):
            print("  Extracted Files & Records:")
            for fname, count in res["records"].items():
                print(f"    * {fname:<25}: {count:,} records")
        else:
            print("  No records extracted.")
    print("=" * 70)


if __name__ == "__main__":
    main()
