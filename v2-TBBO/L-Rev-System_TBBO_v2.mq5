//+------------------------------------------------------------------+
//| L-Rev-System_TBBO_v2.mq5                                         |
//|                                                                  |
//| v2 of the L/CB combined EA, rebuilt from TBBO (trade + BBO)      |
//| research on GC futures, Dec 2025 - Jul 2026 (Databento GLBX).    |
//|                                                                  |
//| WHAT CHANGED vs v1.50 (research summary in the accompanying      |
//| report):                                                         |
//|   1) CB-System (big-candle breakout) is DISABLED by default.     |
//|      Backtest on TBBO-simulated fills: PF 0.79, -3094 pts over   |
//|      6.5 months. The premise is inverted on GC (big candles      |
//|      mean-revert intraday). No filter made it robust. The code   |
//|      remains for reference but should stay off.                  |
//|   2) L-System gains three quality gates that took it from        |
//|      PF 0.97 (breakeven) to PF 1.16 (EA-only) / 1.31 (with the   |
//|      optional TBBO flow gate):                                   |
//|        a) Pending-order age cap: unfilled level orders are       |
//|           cancelled L_OrderMaxAgeHours (35h) after first         |
//|           placement and the level is consumed. Stale levels     |
//|           that fill >35h later averaged -4.45 pts/trade.         |
//|        b) Spread gate: while spread > L_MaxSpread ($0.90) the    |
//|           slot's pending orders are pulled and re-placed when    |
//|           the spread normalizes. Fills during wide-spread        |
//|           (news/illiquid) moments averaged -5.92 pts/trade.      |
//|        c) OPTIONAL flow gate (needs an external order-flow       |
//|           bridge writing a file; see tbbo_flow_bridge.py):       |
//|           synthetic-stop mode that only takes the level break    |
//|           when 30s aggressor imbalance, aligned with trade       |
//|           direction, is inside [L_FlowGateMin, L_FlowGateMax]    |
//|           = [0.0, 0.6]: real but not climactic flow.             |
//|   3) Fixed v1.50 compile errors (assignments to the Shared_RR    |
//|      input inside the Init functions).                           |
//|                                                                  |
//| Exit model is unchanged: SL = Median True Range x multiplier,    |
//| TP = SL x Shared_RR. Validated best at the original defaults     |
//| (M15 1.5x / H1 0.5x / H4 0.5x, RR 2.0).                          |
//+------------------------------------------------------------------+
#property copyright "Reconstruction for personal research"
#property version   "2.00"
#property strict

#include <Trade/Trade.mqh>

#define L_N_TF_SLOTS   3
#define CB_N_TF_SLOTS  3
#define L_MAGIC_OFFSET 4

//===================================================================
//======================= SYSTEM ENABLES ============================
//===================================================================
input group "=== L-System | master control ==="
input bool Enable_L_System = true;

input group "=== Shared risk-reward setting ==="
input double Shared_RR = 2.0;  // TP distance = actual SL distance x RR

input group           "=== L-System | M15 ==="
input bool            L_Enable_M15        = true;
input double          L_M15_SLMultiplier  = 1.5;

input group           "=== L-System | H1 ==="
input bool            L_Enable_H1         = true;
input double          L_H1_SLMultiplier   = 0.5;

input group           "=== L-System | H4 ==="
input bool            L_Enable_H4         = true;
input double          L_H4_SLMultiplier   = 0.5;

input group           "=== L-System | signal / levels ==="
input int             L_FractalMedianBars = 8;       // shared by fractal detection and Median True Range
input double          L_MaxLevelDistance  = 200.0;   // ignore levels further than this ($)
input int             L_LevelMaxAgeHours  = 336;     // drop levels older than this (14 days)

input group           "=== L-System | v2 quality gates (TBBO research) ==="
input int             L_OrderMaxAgeHours  = 35;      // cancel unfilled pending N hours after placement (0 = off)
input double          L_MaxSpread         = 0.90;    // pull pendings while spread ($) exceeds this (0 = off)

input group           "=== L-System | v2 optional TBBO flow gate ==="
input bool            L_UseFlowGate       = false;   // synthetic stops gated by external order-flow feed
input double          L_FlowGateMin       = 0.0;     // min aligned 30s aggressor imbalance at trigger
input double          L_FlowGateMax       = 0.6;     // max aligned 30s aggressor imbalance at trigger
input string          L_FlowFile          = "lcb_flow.txt"; // file in MQL5\Files (or Common) written by bridge
input int             L_FlowMaxAgeSec     = 10;      // reject flow data older than this

input group           "=== L-System | trade management ==="
input double          L_FixedLot         = 0.01;
input long            L_MagicBase        = 26031600;  // M15/H1/H4 use L_MagicBase+4/+5/+6
input string          L_TradeCommentPrefix = "L-Rev";


input group "=== CB-System | master control (KEEP OFF - see header) ==="
input bool Enable_CB_System = false;

input group           "=== CB-System | shared normalization ==="
input int             CB_NormalizationLookback       = 50;
input double          CB_MinNormalizedBody           = 3.0;

input group           "=== CB-System | M15 ==="
input bool            CB_Enable_M15                  = true;
input double          CB_M15_SLMultiplier            = 1.5;

input group           "=== CB-System | H1 ==="
input bool            CB_Enable_H1                   = true;
input double          CB_H1_SLMultiplier             = 0.5;

input group           "=== CB-System | H4 ==="
input bool            CB_Enable_H4                   = true;
input double          CB_H4_SLMultiplier             = 0.5;

input group           "=== CB-System | breakout settings ==="
input double          CB_BuyOffset          = 0.10;
input double          CB_SellOffset         = 0.05;

input group           "=== CB-System | trade management ==="
input double          CB_FixedLot         = 0.01;
input long            CB_MagicBase        = 26032600;
input string          CB_TradeCommentPrefix = "CB-System";


//===================================================================
//======================= SHARED FORMAT HELPER ======================
//===================================================================
string FormatTimeframe(ENUM_TIMEFRAMES tf)
  {
   ENUM_TIMEFRAMES resolved=tf;
   if(resolved==PERIOD_CURRENT)
      resolved=(ENUM_TIMEFRAMES)_Period;

   string value=EnumToString(resolved);
   StringReplace(value,"PERIOD_","");
   return value;
  }

string L_BuildTradeComment(ENUM_TIMEFRAMES tf)
  {
   return L_TradeCommentPrefix+" | "+FormatTimeframe(tf);
  }

string CB_BuildTradeComment(ENUM_TIMEFRAMES tf)
  {
   return CB_TradeCommentPrefix+" | "+FormatTimeframe(tf);
  }

bool MagicRangesOverlap(long firstA,long lastA,long firstB,long lastB)
  {
   return (firstA<=lastB && firstB<=lastA);
  }

//=================================================================== state
CTrade L_trade;

struct L_TFSlot
  {
   bool              enabled;
   ENUM_TIMEFRAMES   tf;
   int               bars;
   double            slMultiplier;
   long              magic;
   datetime          lastBar;
  };

struct L_Level
  {
   int               slot;        // owner timeframe slot
   double            price;       // swing extreme (raw bid-chart price)
   bool              isLow;       // true = swing low (sell side)
   datetime          formed;      // time of the swing bar
   datetime          detected;    // time the level was detected (v2)
   datetime          placed;      // time of FIRST order placement (v2, 0 = never)
   bool              pulled;      // order pulled by spread gate (v2)
   ulong             ticket;      // current pending order ticket
  };

L_TFSlot L_slots[L_N_TF_SLOTS];
L_Level  L_levels[];

//=================================================================== helpers
double L_NormPrice(double p)
  {
   int d=(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   return NormalizeDouble(p,d);
  }

double L_MedianOfArray(double &values[])
  {
   int count=ArraySize(values);
   if(count<1) return 0.0;

   ArraySort(values);

   if((count%2)==1)
      return values[count/2];

   return (values[count/2-1]+values[count/2])/2.0;
  }

// bar 0 = forming (excluded), bar 1 = last closed (excluded),
// bars 2..lookback+1 included; one extra older candle for the first TR.
bool L_CalculateMedianTrueRange(ENUM_TIMEFRAMES tf,
                              int lookback,
                              double &medianTrueRange)
  {
   medianTrueRange=0.0;
   if(lookback<1) return false;

   MqlRates rates[];
   int copied=CopyRates(_Symbol,tf,2,lookback+1,rates);
   if(copied<lookback+1) return false;

   double trueRanges[];
   ArrayResize(trueRanges,lookback);

   for(int k=1;k<=lookback;k++)
     {
      int out=k-1;

      double highLow = rates[k].high-rates[k].low;
      double highPrev= MathAbs(rates[k].high-rates[k-1].close);
      double lowPrev = MathAbs(rates[k].low-rates[k-1].close);

      trueRanges[out]=MathMax(highLow,MathMax(highPrev,lowPrev));
     }

   medianTrueRange=L_MedianOfArray(trueRanges);
   return (medianTrueRange>0.0);
  }

void L_InitSlot(int i,
                bool enabled,
                ENUM_TIMEFRAMES tf,
                int bars,
                double slMultiplier)
  {
   L_slots[i].enabled=enabled;
   L_slots[i].tf=tf;
   L_slots[i].bars=bars;
   L_slots[i].slMultiplier=slMultiplier;
   L_slots[i].magic=L_MagicBase+L_MAGIC_OFFSET+i;
   L_slots[i].lastBar=0;
  }

bool L_IsOurMagic(long magic)
  {
   return (magic>=L_MagicBase+L_MAGIC_OFFSET &&
           magic< L_MagicBase+L_MAGIC_OFFSET+L_N_TF_SLOTS);
  }

bool L_LevelKnown(int slot,double price,bool isLow)
  {
   for(int i=0;i<ArraySize(L_levels);i++)
      if(L_levels[i].slot==slot &&
         L_levels[i].isLow==isLow &&
         MathAbs(L_levels[i].price-price)<0.01)
         return true;

   return false;
  }

void L_RemoveLevelAt(int index)
  {
   int n=ArraySize(L_levels);
   if(index<0 || index>=n) return;

   for(int j=index;j<n-1;j++)
      L_levels[j]=L_levels[j+1];

   ArrayResize(L_levels,n-1);
  }

//=================================================================== v2: flow gate
double   g_flowValue=0.0;
datetime g_flowStamp=0;
datetime g_flowLastRead=0;

// Bridge file format (single line): "<unix_epoch_seconds> <imb_30s>"
// imb_30s = (buy aggressor vol - sell aggressor vol) / total vol, last 30s.
bool L_ReadFlowFile()
  {
   if(TimeCurrent()==g_flowLastRead)
      return (TimeCurrent()-g_flowStamp)<=L_FlowMaxAgeSec; // cache 1 read/sec
   g_flowLastRead=TimeCurrent();

   int h=FileOpen(L_FlowFile,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h==INVALID_HANDLE)
      h=FileOpen(L_FlowFile,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_COMMON);
   if(h==INVALID_HANDLE)
      return false;

   string line=FileReadString(h);
   FileClose(h);

   string parts[];
   if(StringSplit(line,' ',parts)<2)
      return false;

   g_flowStamp=(datetime)StringToInteger(parts[0]);
   g_flowValue=StringToDouble(parts[1]);
   return (TimeCurrent()-g_flowStamp)<=L_FlowMaxAgeSec;
  }

// aligned imbalance: positive when flow agrees with trade direction
bool L_FlowGateOpen(int direction) // +1 buy, -1 sell
  {
   if(!L_ReadFlowFile())
      return false; // no fresh flow data -> do not trade (fail safe)

   double aligned=g_flowValue*direction;
   return (aligned>=L_FlowGateMin && aligned<=L_FlowGateMax);
  }

//===================================================================
//======================= L-SYSTEM CORE =============================
//===================================================================
void L_DetectNewSwings(int slot)
  {
   if(!L_slots[slot].enabled) return;

   ENUM_TIMEFRAMES tf=L_slots[slot].tf;
   datetime barTime=iTime(_Symbol,tf,0);
   if(barTime<=0 || barTime==L_slots[slot].lastBar) return;
   L_slots[slot].lastBar=barTime;

   int swingBars=L_slots[slot].bars;
   int shift=swingBars+1;

   if(Bars(_Symbol,tf)<=shift+swingBars) return;

   double candHigh=iHigh(_Symbol,tf,shift);
   double candLow =iLow (_Symbol,tf,shift);
   datetime candT =iTime(_Symbol,tf,shift);
   if(candT<=0) return;

   bool isSwingHigh=true;
   bool isSwingLow =true;

   for(int k=1;k<=swingBars;k++)
     {
      if(iHigh(_Symbol,tf,shift-k)>candHigh ||
         iHigh(_Symbol,tf,shift+k)>candHigh)
         isSwingHigh=false;

      if(iLow(_Symbol,tf,shift-k)<candLow ||
         iLow(_Symbol,tf,shift+k)<candLow)
         isSwingLow=false;
     }

   if(isSwingLow && !L_LevelKnown(slot,candLow,true))
     {
      L_Level l;
      l.slot=slot;
      l.price=candLow;
      l.isLow=true;
      l.formed=candT;
      l.detected=TimeCurrent();
      l.placed=0;
      l.pulled=false;
      l.ticket=0;

      int n=ArraySize(L_levels);
      ArrayResize(L_levels,n+1);
      L_levels[n]=l;

      PrintFormat("[L%d %s] new swing LOW level %.2f (%s)",
                  slot+1,EnumToString(tf),candLow,TimeToString(candT));
     }

   if(isSwingHigh && !L_LevelKnown(slot,candHigh,false))
     {
      L_Level l;
      l.slot=slot;
      l.price=candHigh;
      l.isLow=false;
      l.formed=candT;
      l.detected=TimeCurrent();
      l.placed=0;
      l.pulled=false;
      l.ticket=0;

      int n=ArraySize(L_levels);
      ArrayResize(L_levels,n+1);
      L_levels[n]=l;

      PrintFormat("[L%d %s] new swing HIGH level %.2f (%s)",
                  slot+1,EnumToString(tf),candHigh,TimeToString(candT));
     }
  }

void L_SweepAndPrune(int slot)
  {
   if(!L_slots[slot].enabled) return;

   double bid=SymbolInfoDouble(_Symbol,SYMBOL_BID);
   datetime now=TimeCurrent();

   for(int i=ArraySize(L_levels)-1;i>=0;i--)
     {
      if(L_levels[i].slot!=slot) continue;

      bool kill=false;

      if(L_levels[i].isLow && bid<L_levels[i].price-0.01)
         kill=true; // low broken

      if(!L_levels[i].isLow && bid>L_levels[i].price+0.01)
         kill=true; // high broken

      if((now-L_levels[i].formed)>L_LevelMaxAgeHours*3600)
         kill=true;

      // v2: order age cap - unfilled order (or armed synthetic level)
      // older than L_OrderMaxAgeHours since first placement is consumed.
      if(L_OrderMaxAgeHours>0)
        {
         datetime ref=(L_levels[i].placed>0 ? L_levels[i].placed
                                            : L_levels[i].detected);
         if(ref>0 && (now-ref)>L_OrderMaxAgeHours*3600)
            kill=true;
        }

      if(kill)
        {
         if(L_levels[i].ticket>0 && OrderSelect(L_levels[i].ticket))
            L_trade.OrderDelete(L_levels[i].ticket);

         L_RemoveLevelAt(i);
        }
     }
  }

//--- v2: pull pending orders while the spread is abnormal (news filter).
//    Pulled levels keep their 'placed' stamp and re-arm when spread is OK.
void L_SpreadGate(int slot,double spread)
  {
   if(L_MaxSpread<=0.0) return;
   if(spread<=L_MaxSpread) return;

   for(int i=0;i<ArraySize(L_levels);i++)
     {
      if(L_levels[i].slot!=slot) continue;
      if(L_levels[i].ticket==0) continue;

      if(OrderSelect(L_levels[i].ticket))
        {
         if(L_trade.OrderDelete(L_levels[i].ticket))
           {
            L_levels[i].ticket=0;
            L_levels[i].pulled=true;
           }
        }
     }
  }

void L_MaintainPendings(int slot)
  {
   if(!L_slots[slot].enabled) return;

   double medianTR=0.0;
   if(!L_CalculateMedianTrueRange(L_slots[slot].tf,
                                L_slots[slot].bars,
                                medianTR))
      return;

   double slDistance=medianTR*L_slots[slot].slMultiplier;
   double tpDistance=slDistance*Shared_RR;
   if(slDistance<=0.0 || tpDistance<=0.0) return;

   double bid   =SymbolInfoDouble(_Symbol,SYMBOL_BID);
   double ask   =SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double spread=ask-bid;

   double point=SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   double stopLevelDist=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;

   // v2: spread gate - pull existing orders and place nothing while wide.
   if(L_MaxSpread>0.0 && spread>L_MaxSpread)
     {
      L_SpreadGate(slot,spread);
      return;
     }

   L_trade.SetExpertMagicNumber(L_slots[slot].magic);

   for(int i=0;i<ArraySize(L_levels);i++)
     {
      if(L_levels[i].slot!=slot) continue;

      if(L_levels[i].ticket>0)
        {
         if(OrderSelect(L_levels[i].ticket))
            continue;

         // order gone and not pulled by us -> filled or externally
         // deleted: consume the level (one order per level).
         L_RemoveLevelAt(i);
         i--;
         continue;
        }

      double dist=L_levels[i].isLow ? (bid-L_levels[i].price)
                                    : (L_levels[i].price-bid);
      if(dist<=0 || dist>L_MaxLevelDistance) continue;

      // v2: synthetic-stop mode when the flow gate is on -----------------
      if(L_UseFlowGate)
        {
         bool trigger=false;
         int direction=0;
         if(L_levels[i].isLow && bid<=L_levels[i].price)
           { trigger=true; direction=-1; }
         if(!L_levels[i].isLow && ask>=L_levels[i].price+spread)
           { trigger=true; direction=1; }

         if(L_levels[i].placed==0)
            L_levels[i].placed=TimeCurrent(); // armed now

         if(!trigger) continue;

         if(!L_FlowGateOpen(direction))
           {
            // level was hit without acceptable flow: consume it,
            // mirroring the one-shot pending-order behaviour.
            L_RemoveLevelAt(i);
            i--;
            continue;
           }

         string cmtS=L_BuildTradeComment(L_slots[slot].tf)+" | flow";
         bool ok=false;
         if(direction<0)
           {
            double sl=L_NormPrice(L_levels[i].price+slDistance);
            double tp=L_NormPrice(L_levels[i].price-tpDistance);
            ok=L_trade.Sell(L_FixedLot,_Symbol,0.0,sl,tp,cmtS);
           }
         else
           {
            double sl=L_NormPrice(L_levels[i].price+spread-slDistance);
            double tp=L_NormPrice(L_levels[i].price+spread+tpDistance);
            ok=L_trade.Buy(L_FixedLot,_Symbol,0.0,sl,tp,cmtS);
           }
         if(ok)
            PrintFormat("[L%d %s] FLOW %s at level %.2f | imb=%.3f",
                        slot+1,EnumToString(L_slots[slot].tf),
                        direction>0?"BUY":"SELL",
                        L_levels[i].price,g_flowValue);
         L_RemoveLevelAt(i);
         i--;
         continue;
        }
      // -------------------------------------------------------------------

      string cmt=L_BuildTradeComment(L_slots[slot].tf);

      if(L_levels[i].isLow)
        {
         double px=L_NormPrice(L_levels[i].price);
         if(bid-px<stopLevelDist) continue;

         double sl=L_NormPrice(px+slDistance);
         double tp=L_NormPrice(px-tpDistance);

         if(L_trade.SellStop(L_FixedLot,px,_Symbol,sl,tp,
                             ORDER_TIME_GTC,0,cmt))
           {
            L_levels[i].ticket=L_trade.ResultOrder();
            if(L_levels[i].placed==0)
               L_levels[i].placed=TimeCurrent();
            L_levels[i].pulled=false;

            PrintFormat("[L%d %s] SELL STOP %.2f | MTR=%.5f SLdist=%.5f TPdist=%.5f",
                        slot+1,EnumToString(L_slots[slot].tf),px,
                        medianTR,slDistance,tpDistance);
           }
        }
      else
        {
         double px=L_NormPrice(L_levels[i].price+spread);
         if(px-ask<stopLevelDist) continue;

         double sl=L_NormPrice(px-slDistance);
         double tp=L_NormPrice(px+tpDistance);

         if(L_trade.BuyStop(L_FixedLot,px,_Symbol,sl,tp,
                            ORDER_TIME_GTC,0,cmt))
           {
            L_levels[i].ticket=L_trade.ResultOrder();
            if(L_levels[i].placed==0)
               L_levels[i].placed=TimeCurrent();
            L_levels[i].pulled=false;

            PrintFormat("[L%d %s] BUY STOP %.2f | MTR=%.5f SLdist=%.5f TPdist=%.5f",
                        slot+1,EnumToString(L_slots[slot].tf),px,
                        medianTR,slDistance,tpDistance);
           }
        }
     }
  }

//=================================================================== CB state
struct CB_TFSlot
  {
   bool              enabled;
   ENUM_TIMEFRAMES   tf;
   long              magic;
   double            slMultiplier;
   datetime          lastBar;
   ulong             pending;
  };

CB_TFSlot  CB_slots[CB_N_TF_SLOTS];
CTrade  CB_trade;

//=================================================================== CB helpers
double CB_NormPrice(double p)
  {
   int d=(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   return NormalizeDouble(p,d);
  }

double CB_MedianOfArray(double &values[])
  {
   int count=ArraySize(values);
   if(count<1) return 0.0;

   ArraySort(values);

   if((count%2)==1)
      return values[count/2];

   return (values[count/2-1]+values[count/2])/2.0;
  }

bool CB_CalculateNormalizationBaselines(ENUM_TIMEFRAMES tf,
                                     int lookback,
                                     double &medianBody,
                                     double &medianTrueRange)
  {
   medianBody=0.0;
   medianTrueRange=0.0;

   if(lookback<1) return false;

   MqlRates rates[];
   int copied=CopyRates(_Symbol,tf,2,lookback+1,rates);
   if(copied<lookback+1) return false;

   double bodies[];
   double trueRanges[];
   ArrayResize(bodies,lookback);
   ArrayResize(trueRanges,lookback);

   for(int k=1;k<=lookback;k++)
     {
      int out=k-1;

      bodies[out]=MathAbs(rates[k].close-rates[k].open);

      double highLow = rates[k].high-rates[k].low;
      double highPrev= MathAbs(rates[k].high-rates[k-1].close);
      double lowPrev = MathAbs(rates[k].low-rates[k-1].close);
      trueRanges[out]=MathMax(highLow,MathMax(highPrev,lowPrev));
     }

   medianBody=CB_MedianOfArray(bodies);
   medianTrueRange=CB_MedianOfArray(trueRanges);

   return (medianBody>0.0 && medianTrueRange>0.0);
  }

void CB_InitSlot(int i,
              bool en,
              ENUM_TIMEFRAMES tf,
              double slMultiplier)
  {
   CB_slots[i].enabled=en;
   CB_slots[i].tf=tf;
   CB_slots[i].magic=CB_MagicBase+i;
   CB_slots[i].slMultiplier=slMultiplier;
   CB_slots[i].lastBar=0;
   CB_slots[i].pending=0;
  }

//=================================================================== CB signal
void CB_CheckNewBarSignal(int i)
  {
   CB_TFSlot s=CB_slots[i];
   if(!s.enabled) return;

   datetime bt=iTime(_Symbol,s.tf,0);
   if(bt<=0 || bt==s.lastBar) return;
   CB_slots[i].lastBar=bt;

   if(s.pending>0 && OrderSelect(s.pending))
      CB_trade.OrderDelete(s.pending);
   CB_slots[i].pending=0;

   double po=iOpen (_Symbol,s.tf,1);
   double pc=iClose(_Symbol,s.tf,1);
   double ph=iHigh (_Symbol,s.tf,1);
   double pl=iLow  (_Symbol,s.tf,1);
   double body=MathAbs(pc-po);

   double medianBody=0.0;
   double medianTrueRange=0.0;
   if(!CB_CalculateNormalizationBaselines(s.tf,
                                       CB_NormalizationLookback,
                                       medianBody,
                                       medianTrueRange))
      return;

   double normalizedBody=body/medianBody;
   if(normalizedBody<CB_MinNormalizedBody)
      return;

   double slDistance=medianTrueRange*s.slMultiplier;
   double tpDistance=slDistance*Shared_RR;
   if(slDistance<=0.0 || tpDistance<=0.0)
      return;

   datetime expiry=bt+PeriodSeconds(s.tf)-1;
   double bid=SymbolInfoDouble(_Symbol,SYMBOL_BID);
   double ask=SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double point=SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   double stopDist=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;

   CB_trade.SetExpertMagicNumber(s.magic);
   string cmt=CB_BuildTradeComment(s.tf);

   PrintFormat("[Slot %d %s] Signal | Body=%.5f MedianBody=%.5f NormBody=%.3f | MedianTR=%.5f SLdist=%.5f TPdist=%.5f",
               i+1,
               EnumToString(s.tf),
               body,
               medianBody,
               normalizedBody,
               medianTrueRange,
               slDistance,
               tpDistance);

   if(pc>po)
     {
      double px=CB_NormPrice(ph+CB_BuyOffset);
      double sl=CB_NormPrice(px-slDistance);
      double tp=CB_NormPrice(px+tpDistance);

      if(px-ask>stopDist &&
         CB_trade.BuyStop(CB_FixedLot,px,_Symbol,sl,tp,
                         ORDER_TIME_SPECIFIED,expiry,cmt))
         CB_slots[i].pending=CB_trade.ResultOrder();
     }
   else if(pc<po)
     {
      double px=CB_NormPrice(pl-CB_SellOffset);
      double sl=CB_NormPrice(px+slDistance);
      double tp=CB_NormPrice(px-tpDistance);

      if(bid-px>stopDist &&
         CB_trade.SellStop(CB_FixedLot,px,_Symbol,sl,tp,
                          ORDER_TIME_SPECIFIED,expiry,cmt))
         CB_slots[i].pending=CB_trade.ResultOrder();
     }
  }

bool CB_IsOurMagic(long magic)
  {
   return (magic>=CB_MagicBase && magic<CB_MagicBase+CB_N_TF_SLOTS);
  }

//===================================================================
//======================= SYSTEM DISABLE CLEANUP ====================
//===================================================================
void L_CancelPendingOrders()
  {
   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      ulong ticket=OrderGetTicket(i);
      if(ticket==0) continue;
      if(OrderGetString(ORDER_SYMBOL)!=_Symbol) continue;

      long magic=OrderGetInteger(ORDER_MAGIC);
      if(!L_IsOurMagic(magic)) continue;

      L_trade.OrderDelete(ticket);
     }
  }

void CB_CancelPendingOrders()
  {
   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      ulong ticket=OrderGetTicket(i);
      if(ticket==0) continue;
      if(OrderGetString(ORDER_SYMBOL)!=_Symbol) continue;

      long magic=OrderGetInteger(ORDER_MAGIC);
      if(!CB_IsOurMagic(magic)) continue;

      CB_trade.OrderDelete(ticket);
     }
  }

//===================================================================
//======================= COMBINED EVENT HANDLERS ===================
//===================================================================
int OnInit()
  {
   L_InitSlot(0,L_Enable_M15,PERIOD_M15,L_FractalMedianBars,L_M15_SLMultiplier);
   L_InitSlot(1,L_Enable_H1, PERIOD_H1, L_FractalMedianBars,L_H1_SLMultiplier);
   L_InitSlot(2,L_Enable_H4, PERIOD_H4, L_FractalMedianBars,L_H4_SLMultiplier);

   CB_InitSlot(0,CB_Enable_M15,PERIOD_M15,CB_M15_SLMultiplier);
   CB_InitSlot(1,CB_Enable_H1, PERIOD_H1, CB_H1_SLMultiplier);
   CB_InitSlot(2,CB_Enable_H4, PERIOD_H4, CB_H4_SLMultiplier);

   long lFirst=L_MagicBase+L_MAGIC_OFFSET;
   long lLast =lFirst+L_N_TF_SLOTS-1;
   long cbFirst=CB_MagicBase;
   long cbLast =cbFirst+CB_N_TF_SLOTS-1;

   if(MagicRangesOverlap(lFirst,lLast,cbFirst,cbLast))
     {
      PrintFormat("ERROR: L-System magic range %d-%d overlaps CB-System range %d-%d. Choose separate MagicBase values.",
                  (int)lFirst,(int)lLast,(int)cbFirst,(int)cbLast);
      return INIT_PARAMETERS_INCORRECT;
     }

   if(Shared_RR<=0.0)
     {
      Print("Shared_RR must be greater than zero.");
      return INIT_PARAMETERS_INCORRECT;
     }

   if(Enable_L_System)
     {
      if(L_FractalMedianBars<3 ||
         L_MaxLevelDistance<=0.0 ||
         L_LevelMaxAgeHours<1 ||
         L_FixedLot<=0.0)
        {
         Print("Invalid L-System shared inputs. Bars must be >= 3; level distance, age and lot must be positive.");
         return INIT_PARAMETERS_INCORRECT;
        }

      if(L_OrderMaxAgeHours<0 || L_MaxSpread<0.0)
        {
         Print("L_OrderMaxAgeHours and L_MaxSpread must be >= 0 (0 disables the gate).");
         return INIT_PARAMETERS_INCORRECT;
        }

      if(L_UseFlowGate && L_FlowGateMax<=L_FlowGateMin)
        {
         Print("Flow gate band invalid: L_FlowGateMax must exceed L_FlowGateMin.");
         return INIT_PARAMETERS_INCORRECT;
        }

      for(int i=0;i<L_N_TF_SLOTS;i++)
        {
         if(!L_slots[i].enabled) continue;

         if(L_slots[i].bars<3 ||
            L_slots[i].slMultiplier<=0.0)
           {
            PrintFormat("Invalid L-System timeframe %d inputs. Bars must be >= 3 and SL multiplier must be > 0.",i+1);
            return INIT_PARAMETERS_INCORRECT;
           }
        }

      for(int a=0;a<L_N_TF_SLOTS;a++)
         for(int b=a+1;b<L_N_TF_SLOTS;b++)
            if(L_slots[a].enabled &&
               L_slots[b].enabled &&
               L_slots[a].tf==L_slots[b].tf)
               PrintFormat("WARNING: L-System slots %d and %d both use %s. Both engines will trade independently.",
                           a+1,b+1,EnumToString(L_slots[a].tf));
     }

   if(Enable_CB_System)
     {
      Print("WARNING: CB-System is enabled. TBBO backtest Dec 2025 - Jul 2026: PF 0.79, -3094 points. Recommended OFF.");

      if(CB_FixedLot<=0.0)
        {
         Print("Invalid CB-System lot setting.");
         return INIT_PARAMETERS_INCORRECT;
        }

      if(CB_NormalizationLookback<3)
        {
         Print("CB_NormalizationLookback must be at least 3.");
         return INIT_PARAMETERS_INCORRECT;
        }

      if(CB_MinNormalizedBody<=0.0)
        {
         Print("CB_MinNormalizedBody must be greater than zero.");
         return INIT_PARAMETERS_INCORRECT;
        }

      for(int i=0;i<CB_N_TF_SLOTS;i++)
        {
         if(!CB_slots[i].enabled) continue;

         if(CB_slots[i].slMultiplier<=0.0)
           {
            PrintFormat("Invalid CB-System SL multiplier in slot %d.",i+1);
            return INIT_PARAMETERS_INCORRECT;
           }
        }

      for(int a=0;a<CB_N_TF_SLOTS;a++)
         for(int b=a+1;b<CB_N_TF_SLOTS;b++)
            if(CB_slots[a].enabled && CB_slots[b].enabled && CB_slots[a].tf==CB_slots[b].tf)
               PrintFormat("WARNING: CB-System slots %d and %d both use %s - the same candle will trigger two orders.",
                           a+1,b+1,EnumToString(CB_slots[a].tf));
     }

   L_trade.SetDeviationInPoints(30);
   CB_trade.SetDeviationInPoints(30);

   if(Enable_L_System)
      EventSetTimer(5);
   else
      L_CancelPendingOrders();

   if(!Enable_CB_System)
      CB_CancelPendingOrders();

   PrintFormat("[L-Rev v2] %s | magic range=%d-%d | orderAgeCap=%dh spreadCap=%.2f flowGate=%s",
               Enable_L_System ? "ENABLED" : "disabled",
               (int)lFirst,(int)lLast,
               L_OrderMaxAgeHours,L_MaxSpread,
               L_UseFlowGate ? "ON" : "off");

   for(int i=0;i<L_N_TF_SLOTS;i++)
      PrintFormat("[L%d] %s | TF=%s | Bars=%d | SLx=%.3f | RR=%.3f | magic=%d",
                  i+1,
                  L_slots[i].enabled ? "ENABLED" : "disabled",
                  EnumToString(L_slots[i].tf),
                  L_slots[i].bars,
                  L_slots[i].slMultiplier,
                  Shared_RR,
                  (int)L_slots[i].magic);

   PrintFormat("[CB-System] %s | magic range=%d-%d",
               Enable_CB_System ? "ENABLED" : "disabled",
               (int)cbFirst,(int)cbLast);

   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTick()
  {
   if(Enable_L_System)
     {
      for(int i=0;i<L_N_TF_SLOTS;i++)
         L_DetectNewSwings(i);

      for(int i=0;i<L_N_TF_SLOTS;i++)
        {
         L_SweepAndPrune(i);
         L_MaintainPendings(i);
        }
     }
   else
     {
      L_CancelPendingOrders();
     }

   if(Enable_CB_System)
     {
      for(int i=0;i<CB_N_TF_SLOTS;i++)
         CB_CheckNewBarSignal(i);
     }
   else
     {
      CB_CancelPendingOrders();
     }
  }

void OnTimer()
  {
   if(!Enable_L_System)
     {
      L_CancelPendingOrders();
      return;
     }

   for(int i=0;i<L_N_TF_SLOTS;i++)
     {
      L_SweepAndPrune(i);
      L_MaintainPendings(i);
     }
  }

//+------------------------------------------------------------------+
