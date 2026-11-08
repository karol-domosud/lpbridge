# profiles/

**Empty on purpose.** A printer does not need a profile — the agent detects
it, asks the container which driver it needs, and serves it. See the "Profiles
are optional" section of the top-level [README](../README.md).

Drop a `<id>.yaml` here only to *override* the automatic behaviour. In the
shipped product the GUI writes these files; by hand the schema is:

```yaml
id: my-printer                 # required; also the queue name + UUID seed
name: My Printer               # shown in the Print dialog
manufacturer: Acme
aliases: [Acme 1234, OEM 5678]

match:                         # how to recognise the device (any of these)
  usb: [{vendor: "0x04e8", product: "0x3441"}]
  device_id: "MDL:ML-2580N"    # substring of the IEEE-1284 device id
  mdns: ["_ipp._tcp"]

driver:
  # pin an exact driver instead of letting the engine's cupsd rank them:
  ppd: "drv:///splix-samsung.drv/ml2580n.ppd"   # a CUPS ppd-name (`lpinfo -m`) or `everywhere`
  make: Samsung                # hints for the ranking if no ppd
  model: ML-2580N
  vendor: canon-ufr2-6.30      # force an entry of agent/data/vendor-drivers.json
  packages:                    # or your own vendor .deb files (installed into the engine)
    - {url: https://vendor.example/driver_amd64.deb, sha256: "…"}

transport:
  kind: usb-raw                # usb-raw | jetdirect | ipp | lpd | dryrun
  # host: 192.168.1.50         # network kinds; the engine's backend connects to it
  # port: 9100
  # serial: "Z1Y2X3"           # pin a specific USB unit

capabilities:                  # rarely needed; overrides derived IPP attrs
  color: false
```

Every field except `id` has a default or is resolved at run time.
