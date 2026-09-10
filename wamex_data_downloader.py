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
"""

import os
import io
import re
import csv
import sys
import time
import json
import zipfile
import argparse
from typing import Union, List, Tuple, Dict, Optional, Any
import requests

BASE_URL = "https://wamexgeochem.net.au"
VALID_LIMITS = [100, 500, 1000, 10000, -1]


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

    @staticmethod
    def format_analyte_selection(analytes: Union[str, List[str], Tuple[str, ...]]) -> str:
        """Format analyte input into a comma-separated string."""
        if not analytes:
            return "All"
        if isinstance(analytes, (list, tuple, set)):
            items = [str(a).strip() for a in analytes if str(a).strip()]
            return ", ".join(items) if items else "All"
        analyte_str = str(analytes).strip()
        return analyte_str if analyte_str else "All"

    @staticmethod
    def format_limit(limit: Union[int, str]) -> str:
        """Validate and map limit value to valid backend options (100, 500, 1000, 10000, -1)."""
        try:
            lim_int = int(limit)
        except (ValueError, TypeError):
            return "-1"

        if lim_int in VALID_LIMITS:
            return str(lim_int)
        if lim_int <= 0:
            return "-1"
        for threshold in [100, 500, 1000, 10000]:
            if lim_int <= threshold:
                return str(threshold)
        return "-1"

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
        area: str = "All",
        analytes: Union[str, List[str]] = "All",
        hole_or_sample_type: str = "All",
        tenement: str = "All",
        company: str = "All",
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

        final_output_dir = output_dir
        if not output_dir.endswith(folder_name):
            final_output_dir = os.path.join(output_dir, folder_name)
        os.makedirs(final_output_dir, exist_ok=True)

        meta_filename = f"{norm_qtype}_execution_metadata.json"
        meta_path = os.path.join(final_output_dir, meta_filename)

        # Build form values
        b1_val, b2_val = self.format_coordinates(boundary_1=boundary_1, boundary_2=boundary_2, bbox=bbox)
        analyte_val = self.format_analyte_selection(analytes)
        limit_val = self.format_limit(limit)

        current_filters = {
            "area": str(area or "All").strip() or "All",
            "analytes": analyte_val,
            "hole_or_sample_type": str(hole_or_sample_type or "All").strip() or "All",
            "tenement": str(tenement or "All").strip() or "All",
            "company": str(company or "All").strip() or "All",
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

        # Check if identical query has already been downloaded to avoid duplicates
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

        page_url = f"{BASE_URL}{page_endpoint}"

        if verbose:
            print(f"\n[1/5] Accessing {norm_qtype.upper()} query interface ({page_url})...")

        page_resp = self.session.get(page_url, timeout=30)
        if page_resp.status_code != 200:
            raise RuntimeError(f"Failed to access query page: HTTP {page_resp.status_code}")

        csrf_match = re.search(r'name="csrf_token"\s+type="hidden"\s+value="([^"]+)"', page_resp.text)
        if not csrf_match:
            raise RuntimeError("Could not find CSRF token on query page.")
        csrf_token = csrf_match.group(1)

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
            search_resp = self.session.post(page_url, data=payload, files=files, timeout=120)
        finally:
            if opened_file:
                opened_file.close()

        if search_resp.status_code != 200:
            raise RuntimeError(f"Search request failed: HTTP {search_resp.status_code}")

        uuid_match = re.search(r"start_download\(['\"]/start_zip/([a-zA-Z0-9_-]+)['\"]", search_resp.text)
        if not uuid_match:
            uuid_match = re.search(r"/start_zip/([a-zA-Z0-9_-]+)", search_resp.text)

        if not uuid_match:
            if "Found No Results" in search_resp.text or "No search conducted" in search_resp.text:
                if verbose:
                    print("[-] No records found matching the specified criteria.")
                return {"status": "no_results", "records": {}, "output_dir": os.path.abspath(final_output_dir)}
            raise RuntimeError("Could not extract query UUID from server response.")

        query_uuid = uuid_match.group(1)
        if verbose:
            print(f"      Query processed by server (Query UUID: {query_uuid})")

        # Request packaging
        if verbose:
            print(f"\n[3/5] Requesting data archive packaging...")
        start_url = f"{BASE_URL}/start_zip/{query_uuid}"
        options_payload = {"options": additional_dh_tables or []}
        self.session.post(start_url, json=options_payload, timeout=30)

        # Poll status
        check_url = f"{BASE_URL}/check_zip/{query_uuid}"
        redirect_path = None
        if verbose:
            print(f"      Waiting for server archive preparation...")

        for attempt in range(1, 90):
            time.sleep(3)
            check_resp = self.session.get(check_url, timeout=25)
            if check_resp.status_code == 200:
                try:
                    status_data = check_resp.json()
                    if status_data.get("redirect"):
                        redirect_path = status_data["redirect"]
                        if verbose:
                            print(f"      Archive ready: {redirect_path}")
                        break
                    elif status_data.get("timeout"):
                        raise TimeoutError("Server reported query exceeded execution time limit.")
                    else:
                        complete = status_data.get("complete", 0)
                        total = status_data.get("total", 0)
                        if verbose:
                            print(f"      Progress: {complete} / {total} records processed...")
                except json.JSONDecodeError:
                    pass

        if not redirect_path:
            raise TimeoutError("Timed out waiting for server to prepare data package.")

        # Download archive
        download_url = f"{BASE_URL}{redirect_path}"
        if verbose:
            print(f"\n[4/5] Downloading archive from {download_url}...")
        dl_resp = self.session.get(download_url, timeout=300)
        if dl_resp.status_code != 200:
            raise RuntimeError(f"Failed to download archive: HTTP {dl_resp.status_code}")
        if verbose:
            print(f"      Downloaded {len(dl_resp.content):,} bytes.")

        # Extract files
        if verbose:
            print(f"\n[5/5] Extracting files to: {os.path.abspath(final_output_dir)}...")

        z = zipfile.ZipFile(io.BytesIO(dl_resp.content))
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

            hole_or_sample = config.get("hole_or_sample_type", "All")
            if dset == "downhole" and "hole_type" in config:
                hole_or_sample = config["hole_type"]
            elif dset == "surface" and "sample_type" in config:
                hole_or_sample = config["sample_type"]

            res = self.download_dataset(
                query_type=dset,
                area=config.get("area", "All"),
                analytes=config.get("analytes", "All"),
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
    area_input = input("\n1:250,000 Mapsheet Area name (e.g. sheet name, or blank for All): ").strip()
    area = area_input if area_input else "All"

    # 3. Analytes
    analytes_input = input("Analytes / Elements (comma-separated, or blank for All): ").strip()
    analytes = analytes_input if analytes_input else "All"

    # 4. Hole or Sample Type
    if query_type == "downhole":
        type_prompt = "Hole Type for Downhole (e.g. RC, DD, Core, or blank for All): "
    elif query_type == "surface":
        type_prompt = "Sample Type for Surface (e.g. SOIL, ROCKCHIP, STREAMSED, or blank for All): "
    else:
        type_prompt = "Hole Type (DH) / Sample Type (Surface) (or blank for All): "
    type_input = input(type_prompt).strip()
    hole_or_sample_type = type_input if type_input else "All"

    # 5. Tenement ID
    tenement_input = input("Tenement ID (e.g. tenement identifier, or blank for All): ").strip()
    tenement = tenement_input if tenement_input else "All"

    # 6. Company Name
    company_input = input("Company Name (partial or complete, or blank for All): ").strip()
    company = company_input if company_input else "All"

    # 7. WAMEX Report Number
    report_number_input = input("WAMEX Report Number (A-Number, or blank for All): ").strip()
    report_number = report_number_input if report_number_input else ""

    # 8. Company Hole ID or Sample ID
    company_id_input = input("Company Hole ID or Sample ID (or blank for All): ").strip()
    company_id = company_id_input if company_id_input else ""

    # 9. Record Limit
    print("\nRecord Limit options: -1 (ALL matching records), 100, 500, 1000, 10000")
    limit_input = input("Enter Record Limit (default: -1 for ALL records): ").strip()
    limit = limit_input if limit_input else "-1"

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
        "area": area,
        "analytes": analytes,
        "hole_or_sample_type": hole_or_sample_type,
        "tenement": tenement,
        "company": company,
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
        "verbose": True
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
            "area": args.area or "All",
            "analytes": args.analytes or "All",
            "hole_or_sample_type": args.hole_or_sample_type or "All",
            "tenement": args.tenement or "All",
            "company": args.company or "All",
            "report_number": args.report_number or "",
            "company_id": args.company_id or "",
            "limit": args.limit or "-1",
            "boundary_1": args.boundary_1,
            "boundary_2": args.boundary_2,
            "bbox": args.bbox,
            "polygon_file": args.polygon_file,
            "min_depth": args.min_depth,
            "max_depth": args.max_depth,
            "additional_dh_tables": [],
            "output_dir": args.output_dir or "WAMEX_data",
            "force_download": args.force,
            "verbose": True
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
