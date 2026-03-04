# FLIR E Series PTU Command Reference Index

## Document Information
- **Manual**: E Series Pan-Tilt Command Reference Manual
- **Version**: 6.00 (09/2014)
- **Models Covered**: PTU-E46, PTU-D48 E Series, PTU-D100 E Series, PTU-D300 E Series

---

## Position Commands (Section 4)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **PP** | Set/query pan position | `PP<position>` or `PP` | 23 |
| **TP** | Set/query tilt position | `TP<position>` or `TP` | 23 |
| **PO** | Set/query pan offset | `PO<position>` or `PO` | 24 |
| **TO** | Set/query tilt offset | `TO<position>` or `TO` | 24 |
| **PR** | Pan resolution | `PR` | 25 |
| **TR** | Tilt resolution | `TR` | 25 |
| **A** | Await position completion | `A` | 29 |
| **I** | Immediate execution mode | `I` | 28 |
| **S** | Slaved execution mode | `S` | 28 |
| **IQ** | Query execution mode | `IQ` | 28 |
| **H** | Halt all movement | `H` | 30 |
| **HP** | Halt pan movement | `HP` | 30 |
| **HT** | Halt tilt movement | `HT` | 30 |
| **M** | Monitor (auto-scan) | `M<positions>` or `M` | 31 |
| **ME** | Monitor auto enable | `ME` | 31 |
| **MD** | Monitor auto disable | `MD` | 31 |
| **MQ** | Monitor auto query | `MQ` | 31 |
| **MS** | Monitor status | `MS` | 33 |
| **XS** | Set preset | `XS<index>` | 34 |
| **XG** | Go to preset | `XG<index>` | 34 |
| **XC** | Clear preset | `XC<index>` | 34 |
| **B** | Query/set PTU motion | `B` or `B<pan>,<tilt>,<pspd>,<tspd>` | 35 |

---

## Speed Control Commands (Section 5)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **PS** | Set/query pan speed | `PS<speed>` or `PS` | 38 |
| **TS** | Set/query tilt speed | `TS<speed>` or `TS` | 38 |
| **PD** | Set/query pan speed offset | `PD<offset>` or `PD` | 39 |
| **TD** | Set/query tilt speed offset | `TD<offset>` or `TD` | 39 |
| **PA** | Pan acceleration | `PA<accel>` or `PA` | 40 |
| **TA** | Tilt acceleration | `TA<accel>` or `TA` | 40 |
| **PB** | Pan base speed | `PB<speed>` or `PB` | 41 |
| **TB** | Tilt base speed | `TB<speed>` or `TB` | 41 |
| **PU** | Pan upper speed | `PU<speed>` or `PU` | 42 |
| **PL** | Pan lower speed | `PL<speed>` or `PL` | 42 |
| **TU** | Tilt upper speed | `TU<speed>` or `TU` | 42 |
| **TL** | Tilt lower speed | `TL<speed>` or `TL` | 42 |
| **C** | Query control mode | `C` | 43 |
| **CI** | Set position control | `CI` | 43 |
| **CV** | Set velocity control | `CV` | 43 |

---

## Continuous Rotation Commands (Section 6)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **PC** | Query pan continuous | `PC` | 47 |
| **PCE** | Pan continuous enable | `PCE` | 47 |
| **PCD** | Pan continuous disable | `PCD` | 47 |

---

## Limit Commands (Section 7)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **PN** | Minimum pan position | `PN` | 26, 50 |
| **PX** | Maximum pan position | `PX` | 26, 50 |
| **TN** | Minimum tilt position | `TN` | 26, 50 |
| **TX** | Maximum tilt position | `TX` | 26, 50 |
| **PNU** | User-defined pan min | `PNU<pos>` or `PNU` | 50 |
| **PXU** | User-defined pan max | `PXU<pos>` or `PXU` | 50 |
| **TNU** | User-defined tilt min | `TNU<pos>` or `TNU` | 50 |
| **TXU** | User-defined tilt max | `TXU<pos>` or `TXU` | 50 |
| **L** | Query limit status | `L` | 27, 51 |
| **LE** | Enable limits | `LE` | 27, 51 |
| **LD** | Disable limits | `LD` | 27, 51 |
| **LU** | User limits | `LU` | 27, 51 |

---

## Unit Control Commands (Section 8)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **R** | Reset PTU | `R` | 55 |
| **RD** | Disable reset | `RD` | 55 |
| **RT** | Reset tilt | `RT` | 55 |
| **RP** | Reset pan | `RP` | 55 |
| **RE** | Reset both axes | `RE` | 55 |
| **RQ** | Query reset type | `RQ` | 55 |
| **RPS** | Query/set pan reset speed | `RPS<speed>` or `RPS` | 55 |
| **RTS** | Query/set tilt reset speed | `RTS<speed>` or `RTS` | 55 |
| **DS** | Default save | `DS` | 57 |
| **DR** | Restore saved settings | `DR` | 57 |
| **DF** | Restore factory defaults | `DF` | 57 |
| **E** | Query echo mode | `E` | 58 |
| **EE** | Enable host cmd echo | `EE` | 58 |
| **ED** | Disable host cmd echo | `ED` | 58 |
| **F** | Query feedback mode | `F` | 59 |
| **FV** | Enable verbose feedback | `FV` | 59 |
| **FT** | Enable terse feedback | `FT` | 59 |
| **V** | Query firmware version | `V` | 60 |
| **VV** | Query short firmware version | `VV` | 60 |
| **VM** | Query PTU model number | `VM` | 60 |
| **VS** | Query PTU serial number | `VS` | 60 |
| **O** | Query VDC and temperature | `O` | 61 |

---

## Power Control Commands (Section 9)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **PH** | Query pan hold power | `PH` | 63 |
| **PHR** | Pan hold power regular | `PHR` | 63 |
| **PHL** | Pan hold power low | `PHL` | 63 |
| **PHO** | Pan hold power off | `PHO` | 63 |
| **TH** | Query tilt hold power | `TH` | 63 |
| **THR** | Tilt hold power regular | `THR` | 63 |
| **THL** | Tilt hold power low | `THL` | 63 |
| **THO** | Tilt hold power off | `THO` | 63 |
| **PM** | Query pan move power | `PM` | 64 |
| **PMH** | Pan move power high | `PMH` | 64 |
| **PMR** | Pan move power regular | `PMR` | 64 |
| **PML** | Pan move power low | `PML` | 64 |
| **TM** | Query tilt move power | `TM` | 64 |
| **TMH** | Tilt move power high | `TMH` | 64 |
| **TMR** | Tilt move power regular | `TMR` | 64 |
| **TML** | Tilt move power low | `TML` | 64 |

---

## Serial & Expanded I/O Commands (Sections 10-11)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **@** | Serial port settings | `@(<baud>,0,<startup>)` | 67 |
| **@A** | Channel A | `@A` or `@A(<params>)` | 69, 71 |
| **@B** | Channel B | `@B` or `@B(<params>)` | 69, 71 |
| **JE** | Joystick enable | `JE` | 19 |
| **JD** | Joystick disable | `JD` | 19 |
| **OOH** | TTL high | `OOH<n>` | 72 |
| **OOL** | TTL low | `OOL<n>` | 72 |

---

## Step Mode Commands (Section 12)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **WP** | Query pan step mode | `WP` | 75 |
| **WPF** | Pan axis full step | `WPF` | 75 |
| **WPH** | Pan axis half step | `WPH` | 75 |
| **WPQ** | Pan axis quarter step | `WPQ` | 75 |
| **WPE** | Pan axis eighth step | `WPE` | 75 |
| **WPA** | Pan axis auto-step | `WPA` | 75 |
| **WT** | Query tilt step mode | `WT` | 75 |
| **WTF** | Tilt axis full step | `WTF` | 75 |
| **WTH** | Tilt axis half step | `WTH` | 75 |
| **WTQ** | Tilt axis quarter step | `WTQ` | 75 |
| **WTE** | Tilt axis eighth step | `WTE` | 75 |
| **WTA** | Tilt axis auto-step | `WTA` | 75 |

---

## PTU Serial Network Commands (Section 13)

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **U** | Query network ID | `U` | 77 |
| **U** | Set network ID | `U<id>` | 77 |
| **_** | Unit select | `_<unit_ID>` | 78 |

---

## IP Network Commands (Section 14) - E Series Only

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **NA** | MAC address | `NA<address>` or `NA` | 81 |
| **NI** | IP address | `NI<address>` or `NI` | 82 |
| **NM** | Query network mode | `NM` | 83 |
| **NMD** | Network mode dynamic | `NMD` | 83 |
| **NMS** | Network mode static | `NMS` | 83 |
| **NR** | Redirect port | `NR[T|U]<port>` | 83 |
| **NG** | Network gateway | `NG<address>` or `NG` | 84 |
| **NN** | Network host name | `NN<name>` or `NN` | 85 |
| **NS** | Network mask | `NS<mask>` or `NS` | 85 |

---

## Control Mode Commands (Section 15) - E Series Only

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **CT** | Query control type | `CT` | 87 |
| **COL** | Open loop control | `COL` | 87 |
| **CEC** | Encoder correction | `CEC` | 87 |
| **CPEC** | Query pan corrections | `CPEC` | 87 |
| **CTEC** | Query tilt corrections | `CTEC` | 87 |

---

## Geo Pointing Module (GPM) Commands (Section 17) - E Series Only

### General GPM Commands
| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **GC** | Calibrate PTU | `GC` | 97 |
| **GCQ** | Query calibration quality | `GCQ` | 97 |
| **GDF** | Reset GPM to factory | `GDF` | 97 |
| **GDR** | Restore last-saved settings | `GDR` | 97 |
| **GDS** | Save all GPM settings | `GDS` | 97 |
| **GS** | GPM status | `GS` | 97 |
| **GPT** | Query/set GPM point type | `GPT<type>` or `GPT` | 97 |

### Position & Altitude Commands
| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **GL** | GPM latitude | `GL<lat>` or `GL` | 99 |
| **GO** | GPM longitude | `GO<lon>` or `GO` | 99 |
| **GA** | GPM altitude | `GA<alt>` or `GA` | 99 |
| **GLLA** | GPM position & altitude | `GLLA<lat>,<lon>,<alt>` or `GLLA` | 99 |

### Orientation Commands
| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **GCP** | GPM camera offset | `GCP<offset>` or `GCP` | 100 |
| **GR** | PTU roll | `GR<roll>` or `GR` | 100 |
| **GP** | PTU pitch | `GP<pitch>` or `GP` | 100 |
| **GY** | PTU yaw | `GY<yaw>` or `GY` | 100 |
| **GRPY** | PTU roll/pitch/yaw | `GRPY<r>,<p>,<y>` or `GRPY` | 100 |

### Landmark Commands
| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **GM** | GPM landmarks | `GM<index>` or `GM` | 102 |
| **GMN** | Number of landmarks | `GMN` | 102 |
| **GMA** | Add landmark | `GMA<name>,<lat>,<lon>,<alt>` | 102 |
| **GMD** | Delete newest landmark | `GMD<index>` or `GMD` | 102 |
| **GMC** | Delete all landmarks | `GMC` | 102 |
| **GG** | Query/set aim landmark | `GG<index>` or `GG<lat>,<lon>,<alt>` or `GG` | 102 |
| **GGD** | Distance to aim point | `GGD<lat>,<lon>,<alt>` or `GGD` | 102 |

---

## Pelco D Commands (Section 18) - E Series Only

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **QP** | Query Pelco D parsing | `QP` | 105 |
| **QPE** | Enable Pelco D parsing | `QPE` | 105 |
| **QPD** | Disable Pelco D parsing | `QPD` | 105 |
| **QA** | Pelco D address | `QA<address>` or `QA` | 105 |

---

## Firewall Commands (Appendix A) - E Series Only

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **NFU** | Push rule to rule list | `NFU<rule>` | 109 |
| **NFO** | Remove/return last rule | `NFO` | 109 |
| **NFF** | Remove all rules | `NFF` | 109 |
| **NFI** | Index | `NFI<n><rule>` or `NFI<n>` | 109 |
| **NFC** | Return number of rules | `NFC` | 109 |
| **NFA** | Apply staging to current | `NFA` | 109 |

---

## Compatibility Mode Commands (Appendix B) - E Series Only

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **CM** | Check compat status | `CM` | 115 |
| **CME** | Enable compat mode | `CME` | 115 |
| **CMD** | Disable compat mode | `CMD` | 115 |

---

## Timestamp Commands (Appendix C) - E Series Only

| Command | Description | Syntax | Page |
|---------|-------------|--------|------|
| **CNT** | Query timestamp count | `CNT` | 119 |
| **CNF** | Query counter frequency | `CNF` | 119 |
| **BT** | PTU speed/pos with timestamp | `BT` | 119 |

---

## Command Syntax Notes

### Basic Syntax Rules
- **Command**: `<command><parameter><delimiter>`
- **Query**: `<command><delimiter>`
- **Delimiter**: Space or Enter
- **Successful command**: Returns `*`
- **Successful query**: Returns `* <result>`
- **Failed command**: Returns `! <error>`

### Parameter Types
- `<position>`: Position in encoder steps
- `<speed>`: Speed in positions per second
- `<delim>`: Delimiter (space or enter)
- `<index>`: Preset number (0-32)
- `<address>`: Network address
- E Series commands marked with **E** icon

### Response Examples
```
PP1000 *           (Command successful)
PP * Current Pan position is 1000    (Query response)
PP3200 ! Maximum allowable Pan position is 3090    (Error)
```

---

## Quick Reference Categories

### **Most Common Commands**
- **PP/TP**: Set pan/tilt position
- **PS/TS**: Set pan/tilt speed  
- **A**: Wait for movement completion
- **H**: Halt all movement
- **R**: Reset/calibrate PTU

### **Essential Setup Commands**
- **V**: Check firmware version
- **L**: Check limit status
- **F**: Set feedback mode
- **DS**: Save current settings

### **Network Commands (E Series)**
- **NI**: Set IP address
- **NN**: Set hostname
- **NA**: Set MAC address

### **GPM Commands (E Series)**
- **GL/GO/GA**: Set GPS position
- **GR/GP/GY**: Set orientation
- **GG**: Point at GPS coordinates