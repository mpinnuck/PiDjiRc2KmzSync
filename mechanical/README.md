# mechanical/

FreeCAD enclosure design for PiDjiRc2KmzSync, to be designed once the
electronics (Pi Zero 2 W + PowerBoost 1000C + 700mAh LiPo + switch +
connectors) are proven working on the bench.

## Known dimensions (as of this scaffold)

| Component | Dimensions |
|---|---|
| Pi Zero 2 W board | 65 x 30 x 5 mm |
| PowerBoost 1000C | ~51 x 26 mm |
| 700mAh LiPo cell | ~50 x 30 x 8 mm |

## Design considerations flagged so far

- Expose micro-USB charge port and charge-status LED externally without
  opening the case
- Heat/venting if left in direct Sydney sun
- USB-C mission-file cable (to RC-2) and micro-USB charge port both need
  external access
- No GPIO header needed — enclosure doesn't need to expose GPIO pins

## Contents (to be added)

- `.FCStd` source file(s)
- Exported `.stl` files for printing
- Any reference sketches/photos used for component footprints
