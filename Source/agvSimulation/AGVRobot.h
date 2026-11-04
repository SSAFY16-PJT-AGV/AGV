
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Pawn.h"
#include "AGVRobot.generated.h"

class UBoxComponent;
class UStaticMeshComponent;
class USpringArmComponent;
class UCameraComponent;
class UInputComponent;
class USphereComponent;
class USceneComponent;
class AAGVGrabbableObject;
class AAGVPlacementPreview;
class AAGVDisplayShelf;

UCLASS()
class AGVSIMULATION_API AAGVRobot : public APawn
{
    GENERATED_BODY()

public:
    AAGVRobot();

    virtual void Tick(float DeltaTime) override;

    virtual void SetupPlayerInputComponent(
        UInputComponent* PlayerInputComponent
    ) override;

    UFUNCTION(BlueprintPure, Category = "Interaction")
    bool CanGrabObject() const;

    UFUNCTION(BlueprintPure, Category = "Interaction")
    bool IsHoldingObject() const;


protected:
    virtual void BeginPlay() override;

    // 충돌 박스
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "AGV")
    UBoxComponent* CollisionBox;

    // Meshy AI Static Mesh
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "AGV")
    UStaticMeshComponent* RobotMesh;

    // 카메라 암
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Camera")
    USpringArmComponent* SpringArm;

    // 카메라
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Camera")
    UCameraComponent* Camera;

    // 이동 속도 (cm/s)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
    float MoveSpeed = 300.0f;

    // 회전 속도 (degree/s)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
    float TurnSpeed = 90.0f;

    // 물체 감지 범위
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Grab")
    USphereComponent* GrabRange;

    // 물체가 부착될 위치
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Grab")
    USceneComponent* GrabPoint;

    // 집기 가능 거리 (cm)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Grab")
    float GrabDistance = 200.0f;

    // 배치 미리보기 블루프린트 클래스
    UPROPERTY(EditDefaultsOnly, Category = "Placement")
    TSubclassOf<AAGVPlacementPreview> PlacementPreviewClass;

    // 현재 생성된 미리보기 액터
    UPROPERTY()
    TObjectPtr<AAGVPlacementPreview> PlacementPreview;

    // 현재 조준 중인 진열대와 슬롯
    UPROPERTY()
    TObjectPtr<AAGVDisplayShelf> AimedShelf;

    int32 AimedSlotIndex = INDEX_NONE;

    // 미리보기 갱신
    void UpdatePlacementPreview();

private:
    float MoveInput = 0.0f;
    float TurnInput = 0.0f;

    void MoveForward(float Value);
    void Turn(float Value);

    void LookRight(float Value);
    void LookUp(float Value);

    void UpdateGrabTarget();

    void GrabObject();
    void ReleaseObject();

    UPROPERTY()
    AAGVGrabbableObject* HeldActor = nullptr;

    UPROPERTY()
    AAGVGrabbableObject* TargetActor = nullptr;
};
