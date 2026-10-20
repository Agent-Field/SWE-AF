// Formatter bodies are verbatim from the pinned CLI; unrelated dependencies
// are stubbed below. Freeze the clock/timezone to make regeneration repeatable.
process.env.TZ = 'UTC';
const NativeDate = Date;
const fixedNow = NativeDate.parse('2026-10-20T16:00:00Z');
global.Date = class extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [fixedNow])); }
  static now() { return fixedNow; }
};
const fs = require('fs');
class OD extends Error {}
const ZB=class ZB extends OD{constructor(H,$,A,L){super(`${ZB.makeMessage(H,$,A)}`);this.status=H,this.headers=L,this.requestID=L?.get("request-id"),this.error=$}static makeMessage(H,$,A){let L=$?.message?typeof $.message==="string"?$.message:JSON.stringify($.message):$?JSON.stringify($):A;if(H&&L)return`${H} ${L}`;if(H)return`${H} status code (no body)`;if(L)return L;return"(no status code or body)"}static generate(H,$,A,L){if(!H||!L)return new p7({message:A,cause:lNH($)});let I=$;if(H===400)return new iNH(H,I,A,L);if(H===401)return new _IH(H,I,A,L);if(H===403)return new nNH(H,I,A,L);if(H===404)return new TIH(H,I,A,L);if(H===409)return new rNH(H,I,A,L);if(H===422)return new aNH(H,I,A,L);if(H===429)return new oNH(H,I,A,L);if(H>=500)return new tNH(H,I,A,L);return new ZB(H,I,A,L)}};
class p7 extends Error {}
class Pb extends p7 {}
class hFH extends Error {}
class ABH extends Error {}
const OI = () => false;
const z9 = () => 'max';
const qW = 'API Error';
const IBH = 'Opus is experiencing high load, please use /model to switch to Sonnet';
const AG = (x) => x;
function hXH(H,$=!1,A=!0){if(!H)return;let L=new Date(H*1000),I=new Date,D=L.getMinutes();if((L.getTime()-I.getTime())/3600000>24){let M={month:"short",day:"numeric",hour:A?"numeric":void 0,minute:!A||D===0?void 0:"2-digit",hour12:A?!0:void 0};if(L.getFullYear()!==I.getFullYear())M.year="numeric";return L.toLocaleString("en-US",M).replace(/ ([AP]M)/i,(U,X)=>X.toLowerCase())+($?` (${Intl.DateTimeFormat().resolvedOptions().timeZone})`:"")}let f=L.toLocaleTimeString("en-US",{hour:"numeric",minute:D===0?void 0:"2-digit",hour12:!0}),E=Intl.DateTimeFormat().resolvedOptions().timeZone;return f.replace(/ ([AP]M)/i,(M,G)=>G.toLowerCase())+($?` (${E})`:"")}
function BL9(H,$){let A=H.resetsAt,L=A?hXH(A,!0):void 0,I=H.overageResetsAt?hXH(H.overageResetsAt,!0):void 0,D=L?` \xB7 resets ${L}`:"";if(H.overageStatus==="rejected"){let B="";if(A&&H.overageResetsAt)if(A<H.overageResetsAt)B=` \xB7 resets ${L}`;else B=` \xB7 resets ${I}`;else if(L)B=` \xB7 resets ${L}`;else if(I)B=` \xB7 resets ${I}`;if(H.overageDisabledReason==="out_of_credits")return`You're out of extra usage${B}`;return _FH("limit",B,$)}if(H.rateLimitType==="seven_day_sonnet"){let B=z9();return _FH(B==="pro"||B==="enterprise"?"weekly limit":"Sonnet limit",D,$)}if(H.rateLimitType==="seven_day_opus")return _FH("Opus limit",D,$);if(H.rateLimitType==="seven_day")return _FH("weekly limit",D,$);if(H.rateLimitType==="five_hour")return _FH("session limit",D,$);return _FH("usage limit",D,$)}
function _FH(H,$,A){return`You've hit your ${H}${$}`}
function AGA(H,$,A){if(H instanceof Pb||H instanceof p7&&H.message.toLowerCase().includes("timeout"))return AG({content:hG$,error:"unknown"});if(H instanceof hFH||H instanceof ABH)return AG({content:WOI()});if(H instanceof Error&&H.message.includes(IBH))return AG({content:IBH,error:"rate_limit"});if(H instanceof ZB&&H.status===429&&wFH(OI())){let L=H.headers?.get?.("anthropic-ratelimit-unified-representative-claim"),I=H.headers?.get?.("anthropic-ratelimit-unified-overage-status");if(L||I){let D={status:"rejected",unifiedRateLimitFallbackAvailable:!1,isUsingOverage:!1},B=H.headers?.get?.("anthropic-ratelimit-unified-reset");if(B)D.resetsAt=Number(B);if(L)D.rateLimitType=L;if(I)D.overageStatus=I;let f=H.headers?.get?.("anthropic-ratelimit-unified-overage-reset");if(f)D.overageResetsAt=Number(f);let E=H.headers?.get?.("anthropic-ratelimit-unified-overage-disabled-reason");if(E)D.overageDisabledReason=E;let M=ZMA(D,$);if(M)return AG({content:M,error:"rate_limit"});return AG({content:DBH,error:"rate_limit"})}return AG({content:`${qW}: Rate limit reached`,error:"rate_limit"})}if(H instanceof Error&&H.message.toLowerCase().includes("prompt is too long"))return AG({content:ca,error:"invalid_request"});if(H instanceof Error&&/maximum of \d+ PDF pages/.test(H.message))return AG({content:R09(),error:"invalid_request"});if(H instanceof Error&&H.message.includes("The PDF specified is password protected"))return AG({content:y09(),error:"invalid_request"});if(H instanceof ZB&&H.status===400&&H.message.includes("image exceeds")&&H.message.includes("maximum"))return AG({content:WOI()});if(H instanceof ZB&&H.status===413)return AG({content:j09(),error:"invalid_request"});if(H instanceof ZB&&H.status===400&&H.message.includes("`tool_use` ids were found without `tool_result` blocks immediately after")){if(A?.messages&&A?.messagesForAPI){let L=H.message.match(/toolu_[a-zA-Z0-9]+/),I=L?L[0]:null;if(I)v09(I,A.messages,A.messagesForAPI)}{let I=sD()?"":" Run /rewind to recover the conversation.";return AG({content:"API Error: 400 due to tool use concurrency issues."+I,error:"invalid_request"})}}if(H instanceof ZB&&H.status===400&&H.message.includes("unexpected `tool_use_id` found in `tool_result`"))l("tengu_unexpected_tool_result",{});if(OI()&&H instanceof ZB&&H.status===400&&H.message.toLowerCase().includes("invalid model name")&&(pXH($)||$==="opus"))return AG({content:"Claude Opus is not available with the Claude Pro plan. If you have updated your subscription plan recently, run /logout and /login for the plan to take effect.",error:"invalid_request"});if(H instanceof Error&&H.message.includes("Your credit balance is too low"))return AG({content:yG$,error:"billing_error"});if(H instanceof Error&&H.message.toLowerCase().includes("x-api-key")){let{source:L}=oZ();return AG({error:"authentication_failed",content:L==="ANTHROPIC_API_KEY"||L==="apiKeyHelper"?bG$:jG$})}if(H instanceof ZB&&H.status===403&&H.message.includes("OAuth token has been revoked"))return AG({error:"authentication_failed",content:vG$});if(H instanceof ZB&&(H.status===401||H.status===403)&&H.message.includes("OAuth authentication is currently not allowed for this organization"))return AG({error:"authentication_failed",content:b09});if(H instanceof ZB&&(H.status===401||H.status===403))return AG({error:"authentication_failed",content:`${qW}: ${H.message} \xB7 Please run /login`});if(c$(process.env.CLAUDE_CODE_USE_BEDROCK)&&H instanceof Error&&H.message.toLowerCase().includes("model id"))return AG({content:`${qW} (${$}): ${H.message}`,error:"invalid_request"});if(H instanceof Error)return AG({content:`${qW}: ${H.message}`,error:"unknown"});return AG({content:qW,error:"unknown"})}

const fixtures=[];
for (const [label, value] of Object.entries({
  session: {rateLimitType:'five_hour'}, weekly:{rateLimitType:'seven_day'},
  opus:{rateLimitType:'seven_day_opus'}, sonnet:{rateLimitType:'seven_day_sonnet'},
  usage:{}, limit:{overageStatus:'rejected'},
  extra:{overageStatus:'rejected',overageDisabledReason:'out_of_credits'},
})) {
  for (const [dateLabel, resetsAt] of Object.entries({none:null, soon:Math.floor(Date.now()/1000)+1000, later:Math.floor(Date.now()/1000)+172800, nextYear:Math.floor(new Date('2028-01-01T15:50:00Z').getTime()/1000)})) {
    fixtures.push({label:label+'-'+dateLabel,reply:BL9({...value,resetsAt}),kind:'cap'});
  }
}
for (const status of [401,403]) {
  for (const [label,error,text] of [
    ['no-body',undefined,undefined],
    ['plain-token-expired',{message:'OAuth token has expired'},undefined],
    ['standard-json',{type:'error',error:{type:'authentication_error',message:'OAuth token has expired'}},undefined],
  ]) {
    const formatted=AGA(new ZB(status,error,text), 'sonnet');
    fixtures.push({label:status+'-'+label,reply:formatted.content,error:formatted.error,kind:'auth'});
  }
}
fs.writeFileSync(require('path').join(__dirname, 'cli-fixtures.json'),JSON.stringify(fixtures,null,2));
console.log(JSON.stringify(fixtures.filter(x=>x.kind==='auth'),null,2));
