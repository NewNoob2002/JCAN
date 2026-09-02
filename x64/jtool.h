#ifndef _JTOOL_H
#define _JTOOL_H
#include <stdint.h>

typedef int BOOL;

typedef enum
{
	dev_all = -1,
	dev_i2c = 0,
	dev_io,
	dev_spi,
	dev_can,
	dev_max,
}dev_type_enum;

typedef enum
{
	ErrNone = 0,//成功
	ErrParam = 1 << 0,//参数错误
	ErrDisconnect = 1 << 1,//USB 断开
	ErrBusy = 1 << 2,//USB发送忙
	ErrWaiting = 1 << 3,//正在等待回复
	ErrTimeOut = 1 << 4,//通信超时
	ErrDataParse = 1 << 5,//通信数据错误
	ErrFailACK = 1 << 6,//返回失败参数
} ErrorType;

typedef enum
{
	INT_NONE = 0,//无
	INT_RISE = 1,//上升沿
	INT_FALL = 2,//下降沿
	INT_HIGH = 3,//高电平
	INT_LOW = 4,//低电平
	INT_RISE_FALL = 5,//双边沿
}INT_TYPE;
//设备操作API
extern __declspec(dllexport) char* DevicesScan(int DevType, int* OutCnt);
extern __declspec(dllexport) void* DevOpen(int DevType, char* Sn, int Id);
extern __declspec(dllexport) BOOL DevClose(void* DevHandle);

//JIO API
extern __declspec(dllexport) ErrorType IOSetNone(void* DevHandle, uint32_t ionum);
extern __declspec(dllexport) ErrorType IOSetNone_m(void* DevHandle, uint32_t iomask);
extern __declspec(dllexport) ErrorType IOSetIn(void* DevHandle, uint32_t ionum, BOOL pullup, BOOL pulldown);
extern __declspec(dllexport) ErrorType IOSetIn_m(void* DevHandle, uint32_t iomask, uint32_t pullups, uint32_t pulldowns);
extern __declspec(dllexport) ErrorType IOSetOut(void* DevHandle, uint32_t ionum, BOOL pp);
extern __declspec(dllexport) ErrorType IOSetOut_m(void* DevHandle, uint32_t iomask, uint32_t pps);
extern __declspec(dllexport) ErrorType IOSetVal(void* DevHandle, uint32_t ionum, BOOL val);
extern __declspec(dllexport) ErrorType IOSetVal_m(void* DevHandle, uint32_t iomask, uint32_t vals);
extern __declspec(dllexport) ErrorType IOSetOutWithVal(void* DevHandle, uint32_t ionum, BOOL pp, BOOL val);
extern __declspec(dllexport) ErrorType IOSetOutWithVal_m(void* DevHandle, uint32_t iomask, uint32_t pps, uint32_t vals);
extern __declspec(dllexport) ErrorType IOPulseOn(void* DevHandle, uint32_t ionum, uint32_t freq);
extern __declspec(dllexport) ErrorType IOPulseOn_m(void* DevHandle, uint32_t iomask, uint32_t* freqs);
extern __declspec(dllexport) ErrorType IOPulseOff(void* DevHandle, uint32_t ionum);
extern __declspec(dllexport) ErrorType IOPulseOff_m(void* DevHandle, uint32_t iomask);
extern __declspec(dllexport) ErrorType IOPulseCnt(void* DevHandle, uint32_t ionum, uint32_t cnt, uint32_t freq);
extern __declspec(dllexport) ErrorType IOPulseCnt_m(void* DevHandle, uint32_t iomask, uint32_t* cnts, uint32_t* freqs);
extern __declspec(dllexport) ErrorType IOPulseFreq(void* DevHandle, uint32_t ionum, uint32_t freq);
extern __declspec(dllexport) ErrorType IOPulseFreq_m(void* DevHandle, uint32_t iomask, uint32_t* freqs);

extern __declspec(dllexport) ErrorType PWMSetFreq(void* DevHandle, uint32_t freq);
extern __declspec(dllexport) ErrorType PWMSetOut(void* DevHandle, uint32_t ionum);
extern __declspec(dllexport) ErrorType PWMSetOut_m(void* DevHandle, uint32_t iomask);
extern __declspec(dllexport) ErrorType PWMSetOn(void* DevHandle, uint32_t ionum);
extern __declspec(dllexport) ErrorType PWMSetOn_m(void* DevHandle, uint32_t iomask);
extern __declspec(dllexport) ErrorType PWMSetOff(void* DevHandle, uint32_t ionum);
extern __declspec(dllexport) ErrorType PWMSetOff_m(void* DevHandle, uint32_t iomask);
extern __declspec(dllexport) ErrorType PWMSetDuty(void* DevHandle, uint32_t ionum, uint16_t duty);
extern __declspec(dllexport) ErrorType PWMSetDuty_m(void* DevHandle, uint32_t iomask, uint16_t* dutys);

extern __declspec(dllexport) ErrorType CapSetIn(void* DevHandle, uint32_t ionum);
extern __declspec(dllexport) ErrorType CapClearCnt(void* DevHandle, uint32_t ionum);

extern __declspec(dllexport) ErrorType ADCSetIn(void* DevHandle, uint32_t ionum, BOOL isdiff);
extern __declspec(dllexport) ErrorType ADCSetIn_m(void* DevHandle, uint32_t iomask, uint32_t isdiffs);
extern __declspec(dllexport) ErrorType ADCSetSamp(void* DevHandle, uint32_t samp);

extern __declspec(dllexport) ErrorType IOGetInVal(void* DevHandle, uint32_t ionum, BOOL* val);
extern __declspec(dllexport) ErrorType IOGetInVal_m(void* DevHandle, uint32_t iomask, uint32_t* vals);
extern __declspec(dllexport) ErrorType IOGetPulseRemain(void* DevHandle, uint32_t ionum, uint32_t* remaincnt);
extern __declspec(dllexport) ErrorType IOGetPulseRemain_m(void* DevHandle, uint32_t iomask, uint32_t* remaincnts);
extern __declspec(dllexport) ErrorType CapGetVal(void* DevHandle, uint32_t ionum, uint32_t* freq, uint16_t* duty, uint32_t* pulsecnt);
extern __declspec(dllexport) ErrorType ADCGetVal(void* DevHandle, uint32_t ionum, uint16_t* adval);
extern __declspec(dllexport) ErrorType ADCGetVal_m(void* DevHandle, uint32_t iomask, uint16_t* advals);

extern __declspec(dllexport) ErrorType JIOReboot(void* DevHandle);
extern __declspec(dllexport) ErrorType JIOSetVcc(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JIOSetVio(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JIOSetHardVersion(void* DevHandle, char* version);
extern __declspec(dllexport) ErrorType JIOSetID(void* DevHandle, uint16_t val);
extern __declspec(dllexport) ErrorType JIOIntoBoot(void* DevHandle);


//JI2C API
typedef enum
{
	REGADDR_NONE = 0,//不发送地址
	REGADDR_8Bit = 1,//发送8位地址
	REGADDR_16Bit = 2,//发送16位地址
	REGADDR_24Bit = 3,//发送24位地址
	REGADDR_32Bit = 4,//发送32位地址
}REGADDR_TYPE;
extern __declspec(dllexport) ErrorType I2CScan(void* DevHandle, uint8_t* cnt, uint8_t* result);
extern __declspec(dllexport) ErrorType I2CWrite(void* DevHandle, uint8_t slave_addr, REGADDR_TYPE reg_type, uint32_t reg_addr, uint16_t len, uint8_t* data);
extern __declspec(dllexport) ErrorType I2CRead(void* DevHandle, uint8_t slave_addr, REGADDR_TYPE reg_type, uint32_t reg_addr, uint16_t len, uint8_t* buf);
extern __declspec(dllexport) ErrorType I2CReadWithDelay(void* DevHandle, uint8_t slave_addr, REGADDR_TYPE reg_type, uint32_t reg_addr, uint16_t len, uint8_t* buf, uint8_t sr_delay, uint8_t raddr_delay);
extern __declspec(dllexport) ErrorType EEWrite(void* DevHandle, uint8_t base_slave_addr, REGADDR_TYPE reg_type, uint16_t page_size, uint32_t reg_addr, uint32_t len, uint8_t* data);
extern __declspec(dllexport) ErrorType EERead(void* DevHandle, uint8_t base_slave_addr, REGADDR_TYPE reg_type, uint32_t reg_addr, uint32_t len, uint8_t* buf);

typedef void (*I2CIntCallbackFun)(void* DevHandle);
extern __declspec(dllexport) ErrorType I2CRegisterIntCallback(void* DevHandle, INT_TYPE inttype, I2CIntCallbackFun callback);
extern __declspec(dllexport) ErrorType I2CCloseIntCallback(void* DevHandle);

extern __declspec(dllexport) ErrorType JI2CReboot(void* DevHandle);
extern __declspec(dllexport) ErrorType JI2CSetVcc(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JI2CSetVio(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JI2CSetSpeed(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JI2CSetHardVersion(void* DevHandle, char* version);
extern __declspec(dllexport) ErrorType JI2CSetID(void* DevHandle, uint16_t val);
extern __declspec(dllexport) ErrorType JI2CIntoBoot(void* DevHandle);

//JSPI API
typedef enum
{
	SINGLEALL = 0,//所有阶段都是单线
	QUADALL = 1,//所有阶段都是四线
	QUADDATA = 2,//仅数据阶段四线，其他单线
	SINGLECMD = 3,//仅指令阶段单线，其他四线
}QSPI_TYPE;
typedef enum
{
	LOW_1EDG = 0,
	LOW_2EDG = 1,
	HIGH_1EDG = 2,
	HIGH_2EDG = 3,
} SPICK_TYPE;
typedef enum
{
	ENDIAN_MSB = 0,//高位在前
	ENDIAN_LSB = 1,//低位在前
} SPIFIRSTBIT_TYPE;
typedef enum
{
	FIELD_NONE,//无
	FIELD_ONE,//1字节
	FIELD_TWO,//2字节
	FIELD_THREE,//3字节
	FIELD_FOUR,//4字节
} FIELDLEN_TYPE;
extern __declspec(dllexport) ErrorType SPIWriteOnly(void* DevHandle, SPICK_TYPE ck, SPIFIRSTBIT_TYPE firstbit, uint32_t len, uint8_t* dataw);
extern __declspec(dllexport) ErrorType SPIReadOnly(void* DevHandle, SPICK_TYPE ck, SPIFIRSTBIT_TYPE firstbit, uint32_t len, uint8_t* bufr);
extern __declspec(dllexport) ErrorType SPIWriteRead(void* DevHandle, SPICK_TYPE ck, SPIFIRSTBIT_TYPE firstbit, uint32_t len, uint8_t* dataw, uint8_t* bufr);

extern __declspec(dllexport) ErrorType QSPIWriteOnly(void* DevHandle, SPICK_TYPE ck, SPIFIRSTBIT_TYPE firstbit, uint32_t len, uint8_t* dataw);
extern __declspec(dllexport) ErrorType QSPIReadOnly(void* DevHandle, SPICK_TYPE ck, SPIFIRSTBIT_TYPE firstbit, uint32_t len, uint8_t* bufr);

extern __declspec(dllexport) ErrorType SPIWriteWithCMD(
	void* DevHandle,
	SPICK_TYPE ck,
	SPIFIRSTBIT_TYPE firstbit,
	QSPI_TYPE qspitype,
	FIELDLEN_TYPE cmdtype,
	uint32_t cmd,
	FIELDLEN_TYPE addrtype,
	uint32_t addr,
	FIELDLEN_TYPE alttype,
	uint32_t alt,
	FIELDLEN_TYPE dummytype,
	uint32_t len,
	uint8_t* dataw);
extern __declspec(dllexport) ErrorType SPIReadWithCMD(
	void* DevHandle,
	SPICK_TYPE ck,
	SPIFIRSTBIT_TYPE firstbit,
	QSPI_TYPE qspitype,
	FIELDLEN_TYPE cmdtype,
	uint32_t cmd,
	FIELDLEN_TYPE addrtype,
	uint32_t addr,
	FIELDLEN_TYPE alttype,
	uint32_t alt,
	FIELDLEN_TYPE dummytype,
	uint32_t len,
	uint8_t* bufr);


typedef void (*SPIIntCallbackFun)(void* DevHandle);
extern __declspec(dllexport) ErrorType SPIRegisterIntCallback(void* DevHandle, INT_TYPE inttype, SPIIntCallbackFun callback);
extern __declspec(dllexport) ErrorType SPICloseIntCallback(void* DevHandle);

extern __declspec(dllexport) ErrorType JSPIReboot(void* DevHandle);
extern __declspec(dllexport) ErrorType JSPISetVcc(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JSPISetVio(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JSPISetSpeed(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JSPISetHardVersion(void* DevHandle, char* version);
extern __declspec(dllexport) ErrorType JSPISetID(void* DevHandle, uint16_t val);
extern __declspec(dllexport) ErrorType JSPIIntoBoot(void* DevHandle);


//JCAN API
typedef enum
{
	CAN_MODE_NORMAL = 0,
	CAN_MODE_SILENT,
	CAN_MODE_LOOPBACK,
	CAN_MODE_SILENT_LOOPBACK,
} CAN_MODE_TYPE;

typedef void (*CANRecvCallbackFun)(void* DevHandle,
	uint8_t CanType, uint8_t Remote, uint8_t Brs, uint8_t IdType,
	uint32_t Id, uint16_t TimestampRaw, uint8_t DataLength, const uint8_t* Data);

extern __declspec(dllexport) ErrorType CANStart(void* DevHandle, CAN_MODE_TYPE Mode);
extern __declspec(dllexport) ErrorType CANStop(void* DevHandle);
extern __declspec(dllexport) ErrorType CANSend(void* DevHandle,
	uint8_t CanType, uint8_t Remote, uint8_t Brs, uint8_t IdType,
	uint32_t Id, uint8_t DataLength, const uint8_t* Data);
extern __declspec(dllexport) ErrorType CANRegisterRecvCallback(void* DevHandle, CANRecvCallbackFun Callback);
extern __declspec(dllexport) ErrorType CANCloseRecvCallback(void* DevHandle);

extern __declspec(dllexport) ErrorType JCANSetNominalSpeed(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JCANSetDataSpeed(void* DevHandle, uint8_t val);
extern __declspec(dllexport) ErrorType JCANSetNominalCustomSpeed(void* DevHandle,
	uint16_t Prescaler, uint16_t Sjw, uint16_t Seg1, uint16_t Seg2);
extern __declspec(dllexport) ErrorType JCANSetDataCustomSpeed(void* DevHandle,
	uint16_t Prescaler, uint16_t Sjw, uint16_t Seg1, uint16_t Seg2);
extern __declspec(dllexport) ErrorType JCANSetFDStandard(void* DevHandle, uint8_t Standard);
extern __declspec(dllexport) ErrorType JCANSetTerminalResistance(void* DevHandle, uint8_t Enable);
extern __declspec(dllexport) ErrorType JCANSetBusOffAutoRecovery(void* DevHandle, uint8_t Enable);
extern __declspec(dllexport) ErrorType JCANSetAutoRetransmission(void* DevHandle, uint8_t Enable);

extern __declspec(dllexport) ErrorType JCANReboot(void* DevHandle);
extern __declspec(dllexport) ErrorType JCANSetHardVersion(void* DevHandle, const char* Version);
extern __declspec(dllexport) ErrorType JCANSetID(void* DevHandle, uint16_t Id);
extern __declspec(dllexport) ErrorType JCANIntoBoot(void* DevHandle);


#endif
