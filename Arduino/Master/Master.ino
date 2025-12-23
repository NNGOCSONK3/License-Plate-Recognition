/*
  MASTER_FIXED.ino  (100% FIX MotorState prototype/order for Arduino preprocessor)

  MASTER FULL (Motor + 2 LCD + RFID IN + RX RFID OUT from SLAVE + 2 Servos + Touch IN/OUT + 1 Buzzer)
  - LCD IN  address: 0x27
  - LCD OUT address: 0x24  (your I2C scan found 0x24 & 0x27)
  - RFID IN (RC522): SS=10 RST=9
  - SLAVE only reads RFID OUT and sends text via UART one-way:
      SLAVE TX(D1) -> MASTER A2 (SoftwareSerial RX)
  - Touch IN  -> A0
  - Touch OUT -> D8
  - Limit switch -> D2 (INPUT_PULLUP), active LOW
  - Motor L298N: ENB=D5(PWM), IN3=D7, IN4=D6
  - Servo IN  -> D3, Servo OUT -> D4 (auto close 3s)
  - Buzzer 2-pin 3.3V: +3.3V -> buzzer -> A1 (sink LOW to beep)

  Serial messages to Python:
    RFID_IN:<uid>
    RFID_OUT:<uid>
    TOUCH_IN
    TOUCH_OUT
    STATION_PASS:<pos>
    ARRIVED:<pos>

  Serial test commands (Serial Monitor 9600):
    PING
    STATUS
    SW?
    SETPOS:3
    GO:2
    1|2|3|4
    M:FWD:200 / M:REV:200 / M:STOP
    OPEN_IN / OPEN_OUT
    LCD1:hello / LCD2:hello
    BEEP:1..5
    RFID_IN:XXXX / RFID_OUT:YYYY (fake)
    OUT,<plate>
*/

#include <Wire.h>
#include <SPI.h>
#include <MFRC522.h>
#include <LiquidCrystal_I2C.h>
#include <SoftwareSerial.h>
#include <Servo.h>

// ======================================================
// ✅ CRITICAL FIX: put enum + prototypes right after includes
//    to beat Arduino's auto-prototype preprocessor
// ======================================================
enum MotorState { IDLE, MOVING_FORWARD, MOVING_BACKWARD };

// Forward declarations (avoid Arduino auto-prototype ordering issues)
int  nextStation(int cur, MotorState dir);
void motorStop();
void motorFwd(int pwm);
void motorRev(int pwm);

void lcdShow(LiquidCrystal_I2C &lcd, const String &l1, const String &l2="");
String uidToString(const MFRC522::Uid &uid);

void buzzerOff();
void buzzerOn();
void playBeep(int count, int onMs=90, int offMs=70);

void openGateIn();
void openGateOut();
void closeGateIn();
void closeGateOut();
void handleGateAutoClose();

void printStatus();
void startMovementTo(int t);
void onArrived();
void onLimitPass();
void checkLimitSwitchDebounced();
void checkMoveTimeout();

void readTouchSensors();
void handleCmd(String cmd);
// ======================================================


// ======================================================
// LCD
// ======================================================
#define LCD_IN_ADDRESS   0x27
#define LCD_OUT_ADDRESS  0x25
LiquidCrystal_I2C lcdIn(LCD_IN_ADDRESS, 16, 2);
LiquidCrystal_I2C lcdOut(LCD_OUT_ADDRESS, 16, 2);

void lcdShow(LiquidCrystal_I2C &lcd, const String &l1, const String &l2) {
  lcd.clear();
  lcd.setCursor(0,0); lcd.print(l1.substring(0,16));
  lcd.setCursor(0,1); lcd.print(l2.substring(0,16));
}

// ======================================================
// RFID IN (RC522)
// ======================================================
#define RFID_SS   10
#define RFID_RST  9
MFRC522 rfidIn(RFID_SS, RFID_RST);

String uidToString(const MFRC522::Uid &uid) {
  String s = "";
  for (byte i = 0; i < uid.size; i++) {
    if (uid.uidByte[i] < 0x10) s += "0";
    s += String(uid.uidByte[i], HEX);
  }
  s.toUpperCase();
  return s;
}

// ======================================================
// SLAVE UART (one-way): SLAVE TX(D1) -> MASTER A2
// ======================================================
SoftwareSerial slaveSerial(A2, A3); // RX=A2, TX=A3 unused
String slaveBuf;

// ======================================================
// Touch
// ======================================================
#define TOUCH_PIN_IN   8
#define TOUCH_PIN_OUT  A0

// ======================================================
// Buzzer (sink low)
// ======================================================
#define BUZZER_PIN  A1
void buzzerOff() { pinMode(BUZZER_PIN, INPUT); }
void buzzerOn()  { pinMode(BUZZER_PIN, OUTPUT); digitalWrite(BUZZER_PIN, LOW); }
void playBeep(int count, int onMs, int offMs) {
  for(int i=0;i<count;i++){
    buzzerOn(); delay(onMs);
    buzzerOff(); delay(offMs);
  }
}

// ======================================================
// Servo
// ======================================================
#define SERVO_PIN_IN   3
#define SERVO_PIN_OUT  4
Servo servoIn, servoOut;

const int SERVO_IN_CLOSED  = 90;
const int SERVO_IN_OPEN    = 30;

const int SERVO_OUT_CLOSED = 0;
const int SERVO_OUT_OPEN   = 70;

const unsigned long GATE_HOLD_TIME = 3000;
unsigned long lastGateInOpenTime = 0, lastGateOutOpenTime = 0;
bool gateInOpen=false, gateOutOpen=false;

void openGateIn() {
  servoIn.write(SERVO_IN_OPEN);
  gateInOpen=true;
  lastGateInOpenTime=millis();
  playBeep(2);
  Serial.println("OK:OPEN_IN");
}
void openGateOut() {
  servoOut.write(SERVO_OUT_OPEN);
  gateOutOpen=true;
  lastGateOutOpenTime=millis();
  playBeep(2);
  Serial.println("OK:OPEN_OUT");
}
void closeGateIn()  { servoIn.write(SERVO_IN_CLOSED);  gateInOpen=false; }
void closeGateOut() { servoOut.write(SERVO_OUT_CLOSED); gateOutOpen=false; }

void handleGateAutoClose() {
  if (gateInOpen && (millis()-lastGateInOpenTime>=GATE_HOLD_TIME)) {
    closeGateIn();
    lcdIn.setCursor(0,1); lcdIn.print("READY           ");
  }
  if (gateOutOpen && (millis()-lastGateOutOpenTime>=GATE_HOLD_TIME)) {
    closeGateOut();
    lcdOut.setCursor(0,1); lcdOut.print("READY           ");
  }
}

// ======================================================
// Motor (L298N)
// ======================================================
#define MOTOR_ENB  5
#define MOTOR_IN3  7
#define MOTOR_IN4  6

void motorStop() {
  analogWrite(MOTOR_ENB, 0);
  digitalWrite(MOTOR_IN3, LOW);
  digitalWrite(MOTOR_IN4, LOW);
  Serial.println("MOTOR:STOP");
}
void motorFwd(int pwm) {
  pwm = constrain(pwm,0,255);
  digitalWrite(MOTOR_IN3, HIGH);
  digitalWrite(MOTOR_IN4, LOW);
  analogWrite(MOTOR_ENB, pwm);
  Serial.print("MOTOR:FWD:"); Serial.println(pwm);
}
void motorRev(int pwm) {
  pwm = constrain(pwm,0,255);
  digitalWrite(MOTOR_IN3, LOW);
  digitalWrite(MOTOR_IN4, HIGH);
  analogWrite(MOTOR_ENB, pwm);
  Serial.print("MOTOR:REV:"); Serial.println(pwm);
}

// ======================================================
// Limit + Position logic
// ======================================================
#define LIMIT_SWITCH_PIN 2
static const int N_STATIONS = 4;
static const int DEFAULT_PWM = 200;
static const unsigned long DEBOUNCE_MS = 35;
static const unsigned long MOVE_TIMEOUT_MS = 12000;

int currentPosition=1, targetPosition=1;
bool isMotorSpinning=false;
MotorState motorState = IDLE;

unsigned long moveStartMs=0;
int stepsNeeded=0, stepsPassed=0;

// queue next target if receiving GO while moving
int pendingTarget = 0; // 0 = none

int switchStableState=HIGH, lastSwitchReading=HIGH;
unsigned long lastDebounceTime=0;

int nextStation(int cur, MotorState dir){
  if(dir==MOVING_FORWARD) return (cur%N_STATIONS)+1;
  else return (cur==1)?N_STATIONS:(cur-1);
}

void printStatus(){
  Serial.print("STATUS:pos="); Serial.print(currentPosition);
  Serial.print(",target="); Serial.print(targetPosition);
  Serial.print(",pending="); Serial.print(pendingTarget);
  Serial.print(",motor="); Serial.print(isMotorSpinning?"RUN":"STOP");
  Serial.print(",dir=");
  if(motorState==MOVING_FORWARD) Serial.print("FWD");
  else if(motorState==MOVING_BACKWARD) Serial.print("REV");
  else Serial.print("IDLE");
  Serial.print(",LIMIT="); Serial.println(digitalRead(LIMIT_SWITCH_PIN)==LOW?1:0);
}

void startMovementTo(int t){
  t = constrain(t,1,N_STATIONS);

  if(isMotorSpinning){
    pendingTarget = t;
    Serial.print("OK:PENDING:"); Serial.println(pendingTarget);
    lcdIn.setCursor(0,1); lcdIn.print("PENDING...      ");
    lcdOut.setCursor(0,1); lcdOut.print("PENDING...      ");
    return;
  }

  if(t==currentPosition){
    Serial.print("ARRIVED:"); Serial.println(currentPosition);
    Serial.println("OK:ALREADY_THERE");
    lcdIn.setCursor(0,1); lcdIn.print("READY           ");
    lcdOut.setCursor(0,1); lcdOut.print("READY           ");
    return;
  }

  targetPosition=t;

  int cw  = (targetPosition - currentPosition + N_STATIONS) % N_STATIONS;
  int ccw = (currentPosition - targetPosition + N_STATIONS) % N_STATIONS;

  if(cw <= ccw){
    motorState=MOVING_FORWARD;
    stepsNeeded=cw;
    motorFwd(DEFAULT_PWM);
  }else{
    motorState=MOVING_BACKWARD;
    stepsNeeded=ccw;
    motorRev(DEFAULT_PWM);
  }

  stepsPassed=0;
  isMotorSpinning=true;
  moveStartMs=millis();

  Serial.print("OK:GO:"); Serial.println(targetPosition);
  lcdIn.setCursor(0,1); lcdIn.print("MOVING...       ");
  lcdOut.setCursor(0,1); lcdOut.print("MOVING...       ");
}

void onArrived(){
  if(pendingTarget>=1 && pendingTarget<=N_STATIONS){
    int t = pendingTarget;
    pendingTarget = 0;
    delay(80);
    startMovementTo(t);
  }else{
    lcdIn.setCursor(0,1); lcdIn.print("READY           ");
    lcdOut.setCursor(0,1); lcdOut.print("READY           ");
  }
}

void onLimitPass(){
  if(!isMotorSpinning) return;

  currentPosition = nextStation(currentPosition, motorState);
  stepsPassed++;

  Serial.print("STATION_PASS:");
  Serial.println(currentPosition);

  if(stepsPassed >= stepsNeeded){
    motorStop();
    isMotorSpinning=false;
    motorState=IDLE;

    Serial.print("ARRIVED:");
    Serial.println(currentPosition);

    lcdIn.setCursor(0,1); lcdIn.print("ARRIVED         ");
    lcdOut.setCursor(0,1); lcdOut.print("ARRIVED         ");

    onArrived();
  }
}

void checkLimitSwitchDebounced(){
  int cur = digitalRead(LIMIT_SWITCH_PIN);
  if(cur != lastSwitchReading) lastDebounceTime = millis();

  if((millis()-lastDebounceTime) > DEBOUNCE_MS){
    if(cur != switchStableState){
      switchStableState = cur;
      if(switchStableState == LOW){
        onLimitPass();
      }
    }
  }
  lastSwitchReading = cur;
}

void checkMoveTimeout(){
  if(!isMotorSpinning) return;
  if(millis()-moveStartMs > MOVE_TIMEOUT_MS){
    motorStop();
    isMotorSpinning=false;
    motorState=IDLE;
    Serial.println("ERR:MOVE_TIMEOUT");
    printStatus();
    lcdIn.setCursor(0,1); lcdIn.print("ERR TIMEOUT     ");
    lcdOut.setCursor(0,1); lcdOut.print("ERR TIMEOUT     ");
  }
}

// ======================================================
// Touch to PC (Python)
// ======================================================
unsigned long lastTouchMs=0;
void readTouchSensors(){
  if(millis()-lastTouchMs < 250) return;

  if(digitalRead(TOUCH_PIN_IN)==HIGH){
    Serial.println("TOUCH_IN");
    playBeep(1);
    lastTouchMs=millis();
  }
  if(digitalRead(TOUCH_PIN_OUT)==HIGH){
    Serial.println("TOUCH_OUT");
    playBeep(1);
    lastTouchMs=millis();
  }
}

// ======================================================
// Serial command handling
// ======================================================
String pcBuf;

void handleCmd(String cmd){
  cmd.trim();
  if(!cmd.length()) return;

  if(cmd=="PING"){ Serial.println("PONG"); return; }
  if(cmd=="STATUS"){ printStatus(); return; }
  if(cmd=="SW?"){ Serial.print("LIMIT="); Serial.println(digitalRead(LIMIT_SWITCH_PIN)==LOW?1:0); return; }

  if(cmd.startsWith("LCD1:")){ lcdShow(lcdIn, "LCD1", cmd.substring(5)); Serial.println("OK:LCD1"); return; }
  if(cmd.startsWith("LCD2:")){ lcdShow(lcdOut,"LCD2", cmd.substring(5)); Serial.println("OK:LCD2"); return; }

  if(cmd.startsWith("BEEP:")){
    int n = cmd.substring(5).toInt(); n = constrain(n,1,5);
    playBeep(n);
    Serial.println("OK:BEEP");
    return;
  }

  if(cmd=="M:STOP"){ motorStop(); return; }
  if(cmd.startsWith("M:FWD:")){ motorFwd(cmd.substring(6).toInt()); return; }
  if(cmd.startsWith("M:REV:")){ motorRev(cmd.substring(6).toInt()); return; }

  if(cmd.startsWith("SETPOS:")){
    int p = cmd.substring(7).toInt();
    currentPosition = constrain(p,1,N_STATIONS);
    pendingTarget=0;
    isMotorSpinning=false;
    motorState=IDLE;
    motorStop();
    Serial.print("OK:SETPOS:"); Serial.println(currentPosition);
    printStatus();
    return;
  }

  if(cmd.startsWith("GO:")){
    int t = cmd.substring(3).toInt();
    startMovementTo(t);
    return;
  }
  if(cmd=="1"||cmd=="2"||cmd=="3"||cmd=="4"){
    startMovementTo(cmd.toInt());
    return;
  }

  if(cmd=="OPEN_IN"){ openGateIn(); return; }
  if(cmd=="OPEN_OUT"){ openGateOut(); return; }

  // OUT,<plate> : update LCD out UI
  if(cmd.startsWith("OUT,")){
    String plate = cmd.substring(4); plate.trim();
    lcdOut.setCursor(0,0); lcdOut.print("CONG RA         ");
    lcdOut.setCursor(0,1); lcdOut.print(plate.substring(0,16));
    Serial.println("OK:OUT_UI");
    return;
  }

  // fake rfid to test python
  if(cmd.startsWith("RFID_IN:")){
    Serial.println(cmd);
    playBeep(1);
    lcdIn.setCursor(0,1); lcdIn.print("RFID OK         ");
    return;
  }
  if(cmd.startsWith("RFID_OUT:")){
    Serial.println(cmd);
    playBeep(1);
    lcdOut.setCursor(0,1); lcdOut.print("RFID OK         ");
    return;
  }

  Serial.print("ERR:UNKNOWN_CMD:");
  Serial.println(cmd);
}

// ======================================================
// setup / loop
// ======================================================
void setup(){
  Serial.begin(9600);
  slaveSerial.begin(9600);

  Wire.begin();
  lcdIn.init(); lcdIn.backlight();
  lcdOut.init(); lcdOut.backlight();

  lcdShow(lcdIn,  "PARKING IN",  "READY");
  lcdShow(lcdOut, "PARKING OUT", "READY");

  SPI.begin();
  rfidIn.PCD_Init();

  servoIn.attach(SERVO_PIN_IN);
  servoOut.attach(SERVO_PIN_OUT);
  servoIn.write(SERVO_IN_CLOSED);
  servoOut.write(SERVO_OUT_CLOSED);

  pinMode(TOUCH_PIN_IN, INPUT);
  pinMode(TOUCH_PIN_OUT, INPUT);

  pinMode(LIMIT_SWITCH_PIN, INPUT_PULLUP);

  pinMode(MOTOR_ENB, OUTPUT);
  pinMode(MOTOR_IN3, OUTPUT);
  pinMode(MOTOR_IN4, OUTPUT);
  motorStop();

  buzzerOff();
  playBeep(1,180,60);

  Serial.println("MASTER_FULL_READY");
  printStatus();
}

void loop(){
  // RFID IN
  if(rfidIn.PICC_IsNewCardPresent() && rfidIn.PICC_ReadCardSerial()){
    String uid = uidToString(rfidIn.uid);
    Serial.print("RFID_IN:"); Serial.println(uid);
    playBeep(1);
    lcdIn.setCursor(0,1); lcdIn.print("RFID OK         ");
    rfidIn.PICC_HaltA();
    rfidIn.PCD_StopCrypto1();
  }

  // receive from SLAVE UART
  while(slaveSerial.available()){
    char c = (char)slaveSerial.read();
    if(c=='\n' || c=='\r'){
      if(slaveBuf.length()){
        String line = slaveBuf;
        slaveBuf="";
        line.trim();
        if(line.length()){
          Serial.println(line);              // forward to python
          if(line.startsWith("RFID_OUT:")){
            playBeep(1);
            lcdOut.setCursor(0,1); lcdOut.print("RFID OK         ");
          }
        }
      }
    }else{
      if(slaveBuf.length()<80) slaveBuf += c;
    }
  }

  // serial from python/PC
  while(Serial.available()){
    char c = (char)Serial.read();
    if(c=='\n' || c=='\r'){
      if(pcBuf.length()){
        String cmd = pcBuf;
        pcBuf="";
        handleCmd(cmd);
      }
    }else{
      if(pcBuf.length()<120) pcBuf += c;
    }
  }

  readTouchSensors();
  checkLimitSwitchDebounced();
  checkMoveTimeout();
  handleGateAutoClose();
}
