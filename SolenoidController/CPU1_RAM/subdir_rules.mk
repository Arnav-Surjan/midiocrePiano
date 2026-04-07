################################################################################
# Automatically-generated file. Do not edit!
################################################################################

# Each subdirectory must supply rules for building sources it contributes
build-238891950: ../c2000.syscfg
	@echo 'SysConfig - building file: "$<"'
	"/Applications/ti/ccs2050/ccs/utils/sysconfig_1.27.0/sysconfig_cli.sh" -s "/Applications/ti/c2000/C2000Ware_26_00_00_00/.metadata/sdk.json" -d "F28P55x" -p "128PDT" -r "F28P55x_128PDT" --script "/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController/c2000.syscfg" -o "syscfg" --compiler ccs
	@echo 'Finished building: "$<"'
	@echo ' '

syscfg/board.c: build-238891950 ../c2000.syscfg
syscfg/board.h: build-238891950
syscfg/board.cmd.genlibs: build-238891950
syscfg/board.opt: build-238891950
syscfg/board.json: build-238891950
syscfg/pinmux.csv: build-238891950
syscfg/device.c: build-238891950
syscfg/device.h: build-238891950
syscfg/c2000ware_libraries.cmd.genlibs: build-238891950
syscfg/c2000ware_libraries.opt: build-238891950
syscfg/c2000ware_libraries.c: build-238891950
syscfg/c2000ware_libraries.h: build-238891950
syscfg/clocktree.h: build-238891950
syscfg: build-238891950

syscfg/%.obj: ./syscfg/%.c $(GEN_OPTS) | $(GEN_FILES) $(GEN_MISC_FILES)
	@echo 'C2000 Compiler - building file: "$<"'
	"/Applications/ti/ccs2050/ccs/tools/compiler/ti-cgt-c2000_25.11.0.LTS/bin/cl2000" -v28 -ml -mt --cla_support=cla2 --float_support=fpu32 --tmu_support=tmu1 --vcu_support=vcrc -Ooff --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController" --include_path="/Applications/ti/c2000/C2000Ware_26_00_00_00" --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController/device" --include_path="/Applications/ti/c2000/C2000Ware_26_00_00_00/driverlib/f28p55x/driverlib/" --include_path="/Applications/ti/ccs2050/ccs/tools/compiler/ti-cgt-c2000_25.11.0.LTS/include" --define=DEBUG --define=RAM --diag_suppress=10063 --diag_warning=225 --diag_wrap=off --display_error_number --gen_func_subsections=on --abi=eabi --preproc_with_compile --preproc_dependency="syscfg/$(basename $(<F)).d_raw" --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController/CPU1_RAM/syscfg" --obj_directory="syscfg" $(GEN_OPTS__FLAG) "$<"
	@echo 'Finished building: "$<"'
	@echo ' '

f28p55x_codestartbranch.obj: /Applications/ti/c2000/C2000Ware_26_00_00_00/device_support/f28p55x/common/source/f28p55x_codestartbranch.asm $(GEN_OPTS) | $(GEN_FILES) $(GEN_MISC_FILES)
	@echo 'C2000 Compiler - building file: "$<"'
	"/Applications/ti/ccs2050/ccs/tools/compiler/ti-cgt-c2000_25.11.0.LTS/bin/cl2000" -v28 -ml -mt --cla_support=cla2 --float_support=fpu32 --tmu_support=tmu1 --vcu_support=vcrc -Ooff --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController" --include_path="/Applications/ti/c2000/C2000Ware_26_00_00_00" --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController/device" --include_path="/Applications/ti/c2000/C2000Ware_26_00_00_00/driverlib/f28p55x/driverlib/" --include_path="/Applications/ti/ccs2050/ccs/tools/compiler/ti-cgt-c2000_25.11.0.LTS/include" --define=DEBUG --define=RAM --diag_suppress=10063 --diag_warning=225 --diag_wrap=off --display_error_number --gen_func_subsections=on --abi=eabi --preproc_with_compile --preproc_dependency="$(basename $(<F)).d_raw" --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController/CPU1_RAM/syscfg" $(GEN_OPTS__FLAG) "$<"
	@echo 'Finished building: "$<"'
	@echo ' '

%.obj: ../%.c $(GEN_OPTS) | $(GEN_FILES) $(GEN_MISC_FILES)
	@echo 'C2000 Compiler - building file: "$<"'
	"/Applications/ti/ccs2050/ccs/tools/compiler/ti-cgt-c2000_25.11.0.LTS/bin/cl2000" -v28 -ml -mt --cla_support=cla2 --float_support=fpu32 --tmu_support=tmu1 --vcu_support=vcrc -Ooff --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController" --include_path="/Applications/ti/c2000/C2000Ware_26_00_00_00" --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController/device" --include_path="/Applications/ti/c2000/C2000Ware_26_00_00_00/driverlib/f28p55x/driverlib/" --include_path="/Applications/ti/ccs2050/ccs/tools/compiler/ti-cgt-c2000_25.11.0.LTS/include" --define=DEBUG --define=RAM --diag_suppress=10063 --diag_warning=225 --diag_wrap=off --display_error_number --gen_func_subsections=on --abi=eabi --preproc_with_compile --preproc_dependency="$(basename $(<F)).d_raw" --include_path="/Users/arnavsurjan/Library/CloudStorage/OneDrive-TheUniversityofTexasatAustin/UT/ASSIGNMENTS/SeniorDesign/midiocrePiano/SolenoidController/CPU1_RAM/syscfg" $(GEN_OPTS__FLAG) "$<"
	@echo 'Finished building: "$<"'
	@echo ' '


