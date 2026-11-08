#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Pawn.h"
#include "WarehouseCameraPawn.generated.h"

class USpringArmComponent;
class UCameraComponent;
class USceneComponent;

UCLASS()
class AGVSIMULATION_API AWarehouseCameraPawn : public APawn
{
    GENERATED_BODY()

public:
    AWarehouseCameraPawn();

    virtual void Tick(float DeltaTime) override;
    virtual void SetupPlayerInputComponent(
        UInputComponent* PlayerInputComponent) override;

protected:
    virtual void BeginPlay() override;

    UPROPERTY(VisibleAnywhere, Category = "Camera")
    USceneComponent* SceneRoot;

    UPROPERTY(VisibleAnywhere, Category = "Camera")
    USpringArmComponent* SpringArm;

    UPROPERTY(VisibleAnywhere, Category = "Camera")
    UCameraComponent* Camera;

    UPROPERTY(EditAnywhere, Category = "Camera|Movement")
    float MoveSpeed = 1200.0f;

    UPROPERTY(EditAnywhere, Category = "Camera|Movement")
    float RotationSpeed = 1.5f;

    UPROPERTY(EditAnywhere, Category = "Camera|Movement")
    float ZoomSpeed = 150.0f;

private:
    void MoveForward(float Value);
    void MoveRight(float Value);
    void TurnCamera(float Value);
    void LookUpCamera(float Value);
    void ZoomCamera(float Value);

    bool bRightMouseDown = false;

    void RightMousePressed();
    void RightMouseReleased();
};