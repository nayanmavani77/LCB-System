//+------------------------------------------------------------------+
//| L-System_and_CB-System_M15_H1_H4_Shared_RR.mq5    |
//|                                                                  |
//| Combined EA containing two fully separate strategy modules:      |
//|   1) L-System                                                    |
//|   2) CB-System (Custom Candle Breakout)                           |
//|                                                                  |
//| Independence rules:                                              |
//|   - separate system Enable/Disable inputs                         |
//|   - separate timeframe inputs and runtime state                  |
//|   - separate magic-number ranges                                 |
//|   - separate CTrade objects                                      |
//|   - separate entry, exit, risk and trade management              |
//|   - no cross-management of orders or positions                   |
//|                                                                  |
//| Trade comments use:                                              |
//|   L-System | M15                                                 |
//|   CB-System | H1                                                 |
//+------------------------------------------------------------------+
#property copyright "Reconstruction for personal research"
#property version   "1.50"
#property strict

#include <Trade/Trade.mqh>

// Exit calculation:
//   SL distance = Median True Range x SL multiplier
//   TP distance = SL distance x RR

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
input int             L_FractalMedianBars = 8;       // shared by fractal detection and Median True Range on M15/H1/H4
input double          L_MaxLevelDistance  = 200.0;   // ignore levels further than this ($)
input int             L_LevelMaxAgeHours  = 336;     // drop levels older than this (14 days)

input group           "=== L-System | trade management ==="
input double          L_FixedLot         = 0.01;
input long            L_MagicBase        = 26031600;  // M15/H1/H4 use L_MagicBase+4/+5/+6
input string          L_TradeCommentPrefix = "L-System";



input group "=== CB-System | master control ==="
input bool Enable_CB_System = true;


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
input double          CB_BuyOffset          = 0.10;  // buy stop = signal high + this
input double          CB_SellOffset         = 0.05;  // sell stop = signal low - this

input group           "=== CB-System | trade management ==="
input double          CB_FixedLot         = 0.01;
input long            CB_MagicBase        = 26032600; // M15/H1/H4 use CB_MagicBase+0/+1/+2
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
   int               bars;          // shared fractal and Median True Range bar count
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

// Return median of a double array. Array is sorted in-place.
double L_MedianOfArray(double &values[])
  {
   int count=ArraySize(values);
   if(count<1) return 0.0;

   ArraySort(values);

   if((count%2)==1)
      return values[count/2];

   return (values[count/2-1]+values[count/2])/2.0;
  }

// Exact MTR method reused from the previous normalized EA.
//
// bar 0 = currently forming candle                  (excluded)
// bar 1 = most recently closed candle               (excluded)
// bars 2..lookback+1                               (included)
//
// One additional older candle is copied only to provide the previous
// close required for the oldest True Range calculation.
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

   // CopyRates gives the oldest copied candle at index 0.
   // rates[0] is the extra older candle.
   // rates[1..lookback] are the N MTR baseline candles.
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
   Shared_RR=rr;
   L_slots[i].magic=L_MagicBase+L_MAGIC_OFFSET+i;
   L_slots[i].lastBar=0;
  }

bool L_IsOurMagic(long magic)
  {
   // Recognize the complete L-System magic-number range.
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

//===================================================================
//======================= L-SYSTEM CORE =============================
//===================================================================

// --- swing detection on new signal bar
//     Same algorithm as the original single-TF L-System, scoped to slot.
void L_DetectNewSwings(int slot)
  {
   if(!L_slots[slot].enabled) return;

   ENUM_TIMEFRAMES tf=L_slots[slot].tf;
   datetime barTime=iTime(_Symbol,tf,0);
   if(barTime<=0 || barTime==L_slots[slot].lastBar) return;
   L_slots[slot].lastBar=barTime;

   int swingBars=L_slots[slot].bars;
   int shift=swingBars+1;

   // Need candidate plus the slot's fractal bars on both sides.
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
      l.ticket=0;

      int n=ArraySize(L_levels);
      ArrayResize(L_levels,n+1);
      L_levels[n]=l;

      PrintFormat("[L%d %s] new swing HIGH level %.2f (%s)",
                  slot+1,EnumToString(tf),candHigh,TimeToString(candT));
     }
  }

// --- invalidate swept / stale levels
//     Same rules as the original L-System, scoped to slot.
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

      if(kill)
        {
         if(L_levels[i].ticket>0 && OrderSelect(L_levels[i].ticket))
            L_trade.OrderDelete(L_levels[i].ticket);

         L_RemoveLevelAt(i);
        }
     }
  }

// --- pending-order maintenance
//     Pending orders are placed directly at the swing levels and remain GTC.
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

   L_trade.SetExpertMagicNumber(L_slots[slot].magic);

   for(int i=0;i<ArraySize(L_levels);i++)
     {
      if(L_levels[i].slot!=slot) continue;

      // Each detected level may create only one pending order.
      // If that order was filled or deleted, consume the level instead of re-arming it.
      if(L_levels[i].ticket>0)
        {
         if(OrderSelect(L_levels[i].ticket))
            continue;

         L_RemoveLevelAt(i);
         i--;
         continue;
        }

      double dist=L_levels[i].isLow ? (bid-L_levels[i].price)
                                    : (L_levels[i].price-bid);
      if(dist<=0 || dist>L_MaxLevelDistance) continue;

      string cmt=L_BuildTradeComment(L_slots[slot].tf);

      if(L_levels[i].isLow)
        {
         // Sell stop directly at the detected swing low.
         double px=L_NormPrice(L_levels[i].price);
         if(bid-px<stopLevelDist) continue;

         double sl=L_NormPrice(px+slDistance);
         double tp=L_NormPrice(px-tpDistance);

         if(L_trade.SellStop(L_FixedLot,px,_Symbol,sl,tp,
                             ORDER_TIME_GTC,0,cmt))
           {
            L_levels[i].ticket=L_trade.ResultOrder();

            PrintFormat("[L%d %s] SELL STOP %.2f | MTR=%.5f SLdist=%.5f TPdist=%.5f",
                        slot+1,EnumToString(L_slots[slot].tf),px,
                        medianTR,slDistance,tpDistance);
           }
        }
      else
        {
         // Swing highs are detected on the bid chart; add spread for the buy-stop trigger.
         double px=L_NormPrice(L_levels[i].price+spread);
         if(px-ask<stopLevelDist) continue;

         double sl=L_NormPrice(px-slDistance);
         double tp=L_NormPrice(px+tpDistance);

         if(L_trade.BuyStop(L_FixedLot,px,_Symbol,sl,tp,
                            ORDER_TIME_GTC,0,cmt))
           {
            L_levels[i].ticket=L_trade.ResultOrder();

            PrintFormat("[L%d %s] BUY STOP %.2f | MTR=%.5f SLdist=%.5f TPdist=%.5f",
                        slot+1,EnumToString(L_slots[slot].tf),px,
                        medianTR,slDistance,tpDistance);
           }
        }
     }
  }

//=================================================================== state
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

//=================================================================== helpers
double CB_NormPrice(double p)
  {
   int d=(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   return NormalizeDouble(p,d);
  }

// Return the median of an array of doubles.
// The input array is sorted in-place.
double CB_MedianOfArray(double &values[])
  {
   int count=ArraySize(values);
   if(count<1) return 0.0;

   ArraySort(values);

   if((count%2)==1)
      return values[count/2];

   return (values[count/2-1]+values[count/2])/2.0;
  }

// Calculate BOTH independent normalization baselines from ONE shared window.
//
// Baseline candles:
//   bar 0 = currently forming candle                  (excluded)
//   bar 1 = signal candle being evaluated             (excluded)
//   bars 2..NormalizationLookback+1                   (included)
//
// For True Range, one additional older candle is copied only to supply
// the previous-close value needed by the oldest baseline candle.
//
// Each timeframe calls this function with its own ENUM_TIMEFRAMES value,
// therefore every timeframe builds its own independent baselines.
bool CB_CalculateNormalizationBaselines(ENUM_TIMEFRAMES tf,
                                     int lookback,
                                     double &medianBody,
                                     double &medianTrueRange)
  {
   medianBody=0.0;
   medianTrueRange=0.0;

   if(lookback<1) return false;

   // Need N baseline candles + 1 older candle for the first TR calculation.
   MqlRates rates[];
   int copied=CopyRates(_Symbol,tf,2,lookback+1,rates);
   if(copied<lookback+1) return false;

   // CopyRates stores the oldest copied bar at index 0.
   // rates[0] is the extra older candle.
   // rates[1..lookback] are exactly the N baseline candles.
   double bodies[];
   double trueRanges[];
   ArrayResize(bodies,lookback);
   ArrayResize(trueRanges,lookback);

   for(int k=1;k<=lookback;k++)
     {
      int out=k-1;

      // Median Body baseline
      bodies[out]=MathAbs(rates[k].close-rates[k].open);

      // True Range baseline
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
   Shared_RR=rr;
   CB_slots[i].lastBar=0;
   CB_slots[i].pending=0;
  }

//=================================================================== signal
void CB_CheckNewBarSignal(int i)
  {
   CB_TFSlot s=CB_slots[i];
   if(!s.enabled) return;

   datetime bt=iTime(_Symbol,s.tf,0);
   if(bt<=0 || bt==s.lastBar) return;
   CB_slots[i].lastBar=bt;

   // Delete any leftover pending order of THIS slot from the previous bar.
   if(s.pending>0 && OrderSelect(s.pending))
      CB_trade.OrderDelete(s.pending);
   CB_slots[i].pending=0;

   // Signal candle = bar 1 (the candle that just closed).
   double po=iOpen (_Symbol,s.tf,1);
   double pc=iClose(_Symbol,s.tf,1);
   double ph=iHigh (_Symbol,s.tf,1);
   double pl=iLow  (_Symbol,s.tf,1);
   double body=MathAbs(pc-po);

   // Calculate this timeframe's baselines using the shared
   // normalization lookback value.
   double medianBody=0.0;
   double medianTrueRange=0.0;
   if(!CB_CalculateNormalizationBaselines(s.tf,
                                       CB_NormalizationLookback,
                                       medianBody,
                                       medianTrueRange))
      return; // insufficient or invalid history

   // Entry normalization
   double normalizedBody=body/medianBody;
   if(normalizedBody<CB_MinNormalizedBody)
      return;

   // SL remains MTR-normalized; TP is derived from the actual SL distance using RR
   double slDistance=medianTrueRange*s.slMultiplier;
   double tpDistance=slDistance*Shared_RR;
   if(slDistance<=0.0 || tpDistance<=0.0)
      return;

   datetime expiry=bt+PeriodSeconds(s.tf)-1; // pending order dies at bar end
   double bid=SymbolInfoDouble(_Symbol,SYMBOL_BID);
   double ask=SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double point=SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   double stopDist=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;

   CB_trade.SetExpertMagicNumber(s.magic);
   string cmt=CB_BuildTradeComment(s.tf);

   // Helpful, compact signal log for backtest/live verification.
   PrintFormat("[Slot %d %s] Signal | Body=%.5f MedianBody=%.5f NormBody=%.3f | MedianTR=%.5f SLdist=%.5f TPdist=%.5f",
               i+1,
               EnumToString(s.tf),
               body,
               medianBody,
               normalizedBody,
               medianTrueRange,
               slDistance,
               tpDistance);

   if(pc>po) // bullish candle -> buy the break of its high
     {
      double px=CB_NormPrice(ph+CB_BuyOffset);
      double sl=CB_NormPrice(px-slDistance);
      double tp=CB_NormPrice(px+tpDistance);

      if(px-ask>stopDist &&
         CB_trade.BuyStop(CB_FixedLot,px,_Symbol,sl,tp,
                         ORDER_TIME_SPECIFIED,expiry,cmt))
         CB_slots[i].pending=CB_trade.ResultOrder();
     }
   else if(pc<po) // bearish candle -> sell the break of its low
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

//=================================================================== ownership helper
bool CB_IsOurMagic(long magic)
  {
   // Recognize the complete CB-System magic-number range.
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
   // Always initialize both modules so magic ownership and management
   // remain deterministic, including after a module is disabled.
   // The only supported timeframes are fixed here: M15, H1 and H4.
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

   PrintFormat("[L-System] %s | magic range=%d-%d",
               Enable_L_System ? "ENABLED" : "disabled",
               (int)lFirst,(int)lLast);

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

   for(int i=0;i<CB_N_TF_SLOTS;i++)
      PrintFormat("[CB%d] %s | TF=%s | Lookback=%d | MinBody=%.3f | SLx=%.3f | RR=%.3f | magic=%d",
                  i+1,
                  CB_slots[i].enabled ? "ENABLED" : "disabled",
                  EnumToString(CB_slots[i].tf),
                  CB_NormalizationLookback,
                  CB_MinNormalizedBody,
                  CB_slots[i].slMultiplier,
                  Shared_RR,
                  (int)CB_slots[i].magic);

   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTick()
  {
   // L-System module: only its own state, orders and positions.
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
      // Disable means no L-System pending order may remain able to fill.
      L_CancelPendingOrders();
     }

   // CB-System module: only its own state, orders and positions.
   if(Enable_CB_System)
     {
      for(int i=0;i<CB_N_TF_SLOTS;i++)
         CB_CheckNewBarSignal(i);
     }
   else
     {
      // Disable means no CB-System pending order may remain able to fill.
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

   // Original L-System timer behavior is preserved.
   for(int i=0;i<L_N_TF_SLOTS;i++)
     {
      L_SweepAndPrune(i);
      L_MaintainPendings(i);
     }
  }

//+------------------------------------------------------------------+
