#!/usr/bin/env bash
# =============================================================================
# Oracle Cloud A1 (Always Free ARM) launch-retry loop.
#
# WHY: free A1 capacity is chronically exhausted — the console throws
# "Out of host capacity" for hours or days. Clicking Create by hand is a lottery
# ticket; this buys tickets continuously across all availability domains until
# one lands, then prints the public IP and exits.
#
# Requires: oci CLI configured (~/.oci/config) — see docs/ORACLE_CLOUD_SETUP.md.
# Idempotent: exits immediately if an instance named $NAME already exists, so
# it can't race a successful manual creation into a duplicate.
#
# Usage:  ./scripts/oci_launch_retry.sh
# Stop:   Ctrl-C (or kill the background job)
# =============================================================================
set -uo pipefail

# --- Tenancy-specific values (discovered via the oci CLI) --------------------
COMPARTMENT="${OCI_COMPARTMENT:-ocid1.tenancy.oc1..aaaaaaaanrd235tgmv3wx25av7iao74ufs6vbzd2nug75ovm4o4266offkva}"
SUBNET="${OCI_SUBNET:-ocid1.subnet.oc1.us-chicago-1.aaaaaaaalzd5cqtmn4gacyo4jrufcmkiaitrahtp5ne3c6ifa3uoxk4pagna}"
IMAGE="${OCI_IMAGE:-ocid1.image.oc1.us-chicago-1.aaaaaaaaol7tabwitv6c7zizomgfsao3cvyf4fl6jcjbv2hpx27h5uuo2cla}"
ADS=("fgzo:US-CHICAGO-1-AD-1" "fgzo:US-CHICAGO-1-AD-2" "fgzo:US-CHICAGO-1-AD-3")

# --- Shape: the FULL Always-Free allotment (2 OCPU / 12 GB as of 2026-07) ----
NAME="${OCI_NAME:-jarvis-core}"
OCPUS="${OCI_OCPUS:-2}"
MEM_GB="${OCI_MEM_GB:-12}"
BOOT_GB="${OCI_BOOT_GB:-100}"        # free tier allows 200 GB total block storage
SSH_KEY="${OCI_SSH_KEY:-$HOME/.ssh/jarvis_cloud.pub}"

# Oracle rate-limits aggressive polling — ~2 attempts/min total is safe.
SLEEP_BETWEEN_ADS="${OCI_SLEEP:-30}"
MAX_ROUNDS="${OCI_MAX_ROUNDS:-400}"  # ~10 h at 3 ADs × 30 s
MAX_TRANSIENT="${OCI_MAX_TRANSIENT:-15}"   # consecutive network failures before giving up

command -v oci >/dev/null || { echo "oci CLI not installed"; exit 1; }
[ -f "$SSH_KEY" ] || { echo "SSH key not found: $SSH_KEY"; exit 1; }

already_exists() {
  local n
  n=$(oci compute instance list -c "$COMPARTMENT" --all \
        --query "length(data[?\"display-name\"=='$NAME' && \"lifecycle-state\"!='TERMINATED'])" \
        --raw-output 2>/dev/null) || return 1
  [ "${n:-0}" != "0" ]
}

report_success() {
  local ocid="$1"
  echo "🎉 LAUNCHED: $ocid"
  echo "Waiting for RUNNING state…"
  oci compute instance action --instance-id "$ocid" --action SOFTRESET >/dev/null 2>&1 || true
  for _ in $(seq 1 60); do
    state=$(oci compute instance get --instance-id "$ocid" --query 'data."lifecycle-state"' --raw-output 2>/dev/null)
    [ "$state" = "RUNNING" ] && break
    sleep 10
  done
  vnic=$(oci compute instance list-vnics --instance-id "$ocid" --query 'data[0]."public-ip"' --raw-output 2>/dev/null)
  echo "PUBLIC_IP=${vnic:-unknown}"
  echo "Next: ssh -i ~/.ssh/jarvis_cloud ubuntu@${vnic:-<ip>}"
}

if already_exists; then
  echo "Instance '$NAME' already exists — nothing to do."
  exit 0
fi

transient=0
echo "Hunting A1 capacity: ${OCPUS} OCPU / ${MEM_GB} GB in ${#ADS[@]} ADs (attempt every ${SLEEP_BETWEEN_ADS}s)…"
attempt=0
for round in $(seq 1 "$MAX_ROUNDS"); do
  for ad in "${ADS[@]}"; do
    attempt=$((attempt + 1))
    out=$(oci compute instance launch \
            --availability-domain "$ad" \
            --compartment-id "$COMPARTMENT" \
            --shape "VM.Standard.A1.Flex" \
            --shape-config "{\"ocpus\":${OCPUS},\"memoryInGBs\":${MEM_GB}}" \
            --image-id "$IMAGE" \
            --subnet-id "$SUBNET" \
            --assign-public-ip true \
            --display-name "$NAME" \
            --boot-volume-size-in-gbs "$BOOT_GB" \
            --ssh-authorized-keys-file "$SSH_KEY" \
            2>&1)
    if echo "$out" | grep -q '"id": "ocid1.instance'; then
      ocid=$(echo "$out" | grep -o '"id": "ocid1\.instance[^"]*"' | head -1 | cut -d'"' -f4)
      report_success "$ocid"
      exit 0
    fi
    # Classify the failure so a real misconfiguration doesn't silently spin.
    if echo "$out" | grep -qi "out of host capacity\|OutOfCapacity"; then
      printf '[%s] attempt %d %s: out of capacity\n' "$(date +%H:%M:%S)" "$attempt" "${ad##*-}"
      transient=0
    elif echo "$out" | grep -qi "LimitExceeded\|QuotaExceeded"; then
      echo "❌ QUOTA/LIMIT problem — not a capacity issue. Stopping:"; echo "$out" | head -5; exit 1
    elif echo "$out" | grep -qi "TooManyRequests\|429"; then
      printf '[%s] rate-limited — backing off 120s\n' "$(date +%H:%M:%S)"; sleep 120
      transient=0
    elif echo "$out" | grep -qiE "timed out|timeout|ConnectionError|RequestException|Could not connect|ServiceError.*5[0-9][0-9]|Temporary failure|network is unreachable"; then
      # Laptop slept, Wi-Fi blipped, or OCI had a wobble. NOT fatal — an
      # overnight hunt must survive these (this killed a 673-attempt run).
      transient=$((transient + 1))
      printf '[%s] transient network error (%d/%d) — retrying\n' \
             "$(date +%H:%M:%S)" "$transient" "$MAX_TRANSIENT"
      if [ "$transient" -ge "$MAX_TRANSIENT" ]; then
        echo "❌ $MAX_TRANSIENT consecutive network failures — is the machine offline? Stopping."
        exit 1
      fi
      sleep 60
    else
      echo "❌ Unexpected error — stopping so it can be fixed:"; echo "$out" | head -12; exit 1
    fi
    sleep "$SLEEP_BETWEEN_ADS"
  done
done
echo "Gave up after $attempt attempts. Re-run to keep hunting."
exit 2
