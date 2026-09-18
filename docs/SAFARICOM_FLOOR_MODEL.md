# Safaricom floor model (aligned from NOC experience)

## Network scale
- ~**7,000** sites  
- **50M+** subscribers  
- **Six** geographical regions  

## Regions & responsibility

| Region code | Name | Power / passive | Transmission / fibre | Radio OEM notes |
|-------------|------|-----------------|----------------------|-----------------|
| `NBI_E` | Nairobi East | **Egypro**, Remote Egypro | **Egypro Fibre**, Camusat, Ecta | Mixed |
| `NBI_W` | Nairobi West | ATC, Camusat, Egypro (ATC on some power) | Camusat, Ecta, Adrian Fibre, Alan Dick, Egypro Fibre | **Huawei sites only** → radio → Huawei Radio |
| `MTK` | Mt Kenya | **Egypro**, Remote Egypro | **Soliton** (TX), Egypro Fibre, Camusat | Mixed |
| `CST` | Coast | ATC, Camusat, Egypro | Camusat, Ecta, Adrian, Alan Dick… | **Huawei sites only** |
| `RFT` | Rift Valley | **Tetranet** | Camusat, Ecta, Egypro Fibre | Mixed |
| `WNY` | Western-Nyanza | **Tetranet** | Camusat, Ecta, Egypro Fibre | **Nokia + Huawei** mix |

Fibre ecosystem called out: Ecta, Camusat, Egypro Fibre, Soliton (Mt Kenya TX), Adrian Fibre, Alan Dick; ATC on some power sites.

## Priority (subscribers affected)
| Priority | Users |
|----------|-------|
| **P4** | &lt; 50,000 |
| **P3** | &lt; 100,000 (and ≥ 50,000) |
| **P2** | ≥ 100,000 and &lt; 500,000 |
| **P1** | ≥ 500,000 |

HUB site-type floor remains **P2**; CORE floor **P1**.

## Ticket fields (system + MSP lifecycle)

### System / agent filled at open
- **Incident number** — 9 characters, `INC` + 6 digits (`INC000001`)
- **Priority** P1–P4  
- **Responsible MSP** (power MSP if power; fibre/TX MSP if transmission; Huawei Radio if radio in Huawei regions)  
- **Field engineer** (demo names per region)  
- **Failure time**  
- **Expected resolution time** (from priority SLA restore target)  
- **Time escalated to MSP/FE**  
- Site, region, TT category, RNIO, narrative, etc.

### MSP filled while resolving → closure
- Vendor TT reference  
- Root cause  
- Action taken  
- % complete  
- ETA (optional)  
- Work notes  
- Restored flag → status RESTORED  
- NOC close → CLOSED  

## Config source of truth
`config/operators/safaricom.yaml`
