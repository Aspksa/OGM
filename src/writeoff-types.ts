// Shared contracts for monthly writeoff; no runtime logic.
export type Manual={date:string;kind:string;liters:number;fuel_type:string;organization:string;document:string;confirmed:boolean;note:string};
export type Payload={manual?:Manual[];[key:string]:unknown};
export type Result={start:number|null;end:number|null;raw_km:number|null;km:number|null;card:number;manual_in:number;manual_out:number;unconfirmed:number;actual:number|null;norm:number|null;difference:number|null;flags:string[]};
export type CardChoice={id:number;number:string;employee_id:number|null;liters:Record<string,number>;operations:number;unassigned:number};
export type CurrentCard={id:number;number:string;liters:Record<string,number>;operations:number;pending:number;conflict:boolean};
export type FuelOperation={id:number;card_number:string;moment:string;operation:string;station:string;product:string;category:string;price:number;quantity:number;amount:number;flags:string[]};
export type Vehicle={previous_closed?:boolean;fuel_history?:FuelOperation[];previous_odo?:number|null;auto_season?:string;current_cards?:CurrentCard[];card_details?:{number:string;liters:number;operations:number}[];suggested_card_ids?:number[];id:number;plate:string;model:string;fuel_type:string;driver:string;revision:number;payload:Payload;previous:number|null;result:Result};
export type SourceRow={row:number;plate:string;model:string;category:string;driver:string;values:(number|null)[];note:string;flags?:string[];vehicle_id:number|null};
export type Report={fuel_months?:string[];cards?:CardChoice[];month:string;closed?:boolean;closed_at?:string;rows:Vehicle[];unassigned:number;sources:{id:number;name:string;rows:SourceRow[]}[];audit:{id:number;vehicle_id:number;at:string;reason:string;before_json:string|null;after_json:string}[]};
export type MonthStatus={month:string;statement_loaded:boolean;operations:number;unassigned:number;blocked:number|null;review:number|null;closed:boolean;ready:boolean;draft:boolean};
export type CheckRow={id:number;blocked:string[];warnings:string[]};
export type Readiness={ready:boolean;closed:boolean;unassigned:number;blocked:number;review:number;rows:CheckRow[]};
export type Workspace={report:Report;readiness:Readiness;months:MonthStatus[]};
export type InlineKey="start"|"odo_first"|"odo_last"|"end"|"season";
export type InlineValues=Partial<Record<InlineKey,string>>;
