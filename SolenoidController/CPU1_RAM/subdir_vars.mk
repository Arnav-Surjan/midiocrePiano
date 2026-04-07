################################################################################
# Automatically-generated file. Do not edit!
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
CMD_SRCS += \
../28p55x_generic_ram_lnk.cmd 

SYSCFG_SRCS += \
../c2000.syscfg 

LIB_SRCS += \
/Applications/ti/c2000/C2000Ware_26_00_00_00/driverlib/f28p55x/driverlib/ccs/Debug/driverlib.lib 

ASM_SRCS += \
/Applications/ti/c2000/C2000Ware_26_00_00_00/device_support/f28p55x/common/source/f28p55x_codestartbranch.asm 

C_SRCS += \
./syscfg/board.c \
./syscfg/device.c \
./syscfg/c2000ware_libraries.c \
../c2000_solenoid_controller.c 

GEN_FILES += \
./syscfg/board.c \
./syscfg/board.opt \
./syscfg/device.c \
./syscfg/c2000ware_libraries.opt \
./syscfg/c2000ware_libraries.c 

GEN_MISC_DIRS += \
./syscfg 

C_DEPS += \
./syscfg/board.d \
./syscfg/device.d \
./syscfg/c2000ware_libraries.d \
./c2000_solenoid_controller.d 

GEN_OPTS += \
./syscfg/board.opt \
./syscfg/c2000ware_libraries.opt 

OBJS += \
./syscfg/board.obj \
./syscfg/device.obj \
./syscfg/c2000ware_libraries.obj \
./f28p55x_codestartbranch.obj \
./c2000_solenoid_controller.obj 

ASM_DEPS += \
./f28p55x_codestartbranch.d 

GEN_MISC_FILES += \
./syscfg/board.h \
./syscfg/board.cmd.genlibs \
./syscfg/board.json \
./syscfg/pinmux.csv \
./syscfg/device.h \
./syscfg/c2000ware_libraries.cmd.genlibs \
./syscfg/c2000ware_libraries.h \
./syscfg/clocktree.h 

GEN_MISC_DIRS__QUOTED += \
"syscfg" 

OBJS__QUOTED += \
"syscfg/board.obj" \
"syscfg/device.obj" \
"syscfg/c2000ware_libraries.obj" \
"f28p55x_codestartbranch.obj" \
"c2000_solenoid_controller.obj" 

GEN_MISC_FILES__QUOTED += \
"syscfg/board.h" \
"syscfg/board.cmd.genlibs" \
"syscfg/board.json" \
"syscfg/pinmux.csv" \
"syscfg/device.h" \
"syscfg/c2000ware_libraries.cmd.genlibs" \
"syscfg/c2000ware_libraries.h" \
"syscfg/clocktree.h" 

C_DEPS__QUOTED += \
"syscfg/board.d" \
"syscfg/device.d" \
"syscfg/c2000ware_libraries.d" \
"c2000_solenoid_controller.d" 

GEN_FILES__QUOTED += \
"syscfg/board.c" \
"syscfg/board.opt" \
"syscfg/device.c" \
"syscfg/c2000ware_libraries.opt" \
"syscfg/c2000ware_libraries.c" 

ASM_DEPS__QUOTED += \
"f28p55x_codestartbranch.d" 

SYSCFG_SRCS__QUOTED += \
"../c2000.syscfg" 

C_SRCS__QUOTED += \
"./syscfg/board.c" \
"./syscfg/device.c" \
"./syscfg/c2000ware_libraries.c" \
"../c2000_solenoid_controller.c" 

ASM_SRCS__QUOTED += \
"/Applications/ti/c2000/C2000Ware_26_00_00_00/device_support/f28p55x/common/source/f28p55x_codestartbranch.asm" 


